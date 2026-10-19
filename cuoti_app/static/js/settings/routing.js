(function initSettingsRouting(){
'use strict';
/* ② 模型分工：两条泳道，任务卡点一下就换道并立即保存。隐藏的 ai-route-* 下拉框仍是分工的数据源。
   连续点击时只排一个保存队列：还没保存的换道意图在每次读回设置后重新套上，界面不会闪回。 */
const settingsPage=window.Cuoti.settings;
const settingsRender=settingsPage.render,ui=settingsPage.ui;
const {h}=ui;
const TASKS=settingsRender.ROUTING_TASKS;
const TIER_NAME=Object.freeze({fast:'快速',quality:'高质量'});
const el=id=>document.getElementById(id);
const selectOf=task=>el(settingsRender.fields[task.key]);
const tierOf=task=>{
  const selected=selectOf(task)?.value;
  return selected==='fast'||selected==='quality'?selected:task.rec;
};
const intents=new Map();
let saving=false,lastAction='move';
const loaded=()=>Boolean(settingsPage.connection?.isLoaded?.());

function chip(task){
  const tier=tierOf(task),other=tier==='fast'?'quality':'fast';
  return h('button',{type:'button',class:`task-chip${intents.has(task.key)?' is-saving':''}`,dataset:{task:task.key},
    'aria-label':`${task.label}：当前用${TIER_NAME[tier]}模型${task.rec===tier?'（推荐）':''}，点按改用${TIER_NAME[other]}模型`,
    title:`点一下改用${TIER_NAME[other]}模型`,onclick:()=>moveTask(task.key)},
  ui.iconNode(task.icon),task.label,task.rec===tier?h('em',{text:'推荐'}):null,
  h('span',{class:'task-chip-swap','aria-hidden':'true',html:ui.iconHTML('repeat')}));
}

function render(){
  // 重画任务卡会换掉节点：焦点在哪张卡上，画完还落回那张卡（键盘连续换道不丢焦点）
  const focused=document.activeElement?.closest?.('.task-chip[data-task]')?.dataset.task;
  const models=settingsPage.connection?.savedModels?.()||{loaded:false};
  const fastModel=el('lane-fast-model'),qualityModel=el('lane-quality-model');
  if(fastModel)fastModel.textContent=models.loaded?(models.fast||'未设置'):'';
  if(qualityModel)qualityModel.textContent=models.loaded?(models.quality||'未设置'):'';
  for(const tier of ['fast','quality']){
    const body=document.querySelector(`[data-lane-body="${tier}"]`);
    if(!body)continue;
    if(!models.loaded){body.replaceChildren(h('span',{class:'lane-empty',text:'正在读取…'}));continue}
    const chips=TASKS.filter(task=>tierOf(task)===tier).map(chip);
    body.replaceChildren(...(chips.length?chips:[h('span',{class:'lane-empty',text:'暂无任务 · 点另一侧的任务卡移过来'})]));
  }
  const reset=el('routing-reset');
  if(reset)reset.hidden=!models.loaded||TASKS.every(task=>tierOf(task)===task.rec);
  if(focused)document.querySelector(`.task-chip[data-task="${focused}"]`)?.focus({preventScroll:true});
  ui.changed();
}

/* 换道动画：记下每张卡的旧位置，重排后从旧位置滑过去（减少动态效果时直接到位） */
function flip(mutate){
  const chips=()=>[...document.querySelectorAll('.task-chip[data-task]')];
  const before=new Map(chips().map(node=>[node.dataset.task,node.getBoundingClientRect()]));
  mutate();
  if(ui.prefersReduced())return;
  chips().forEach(node=>{
    const first=before.get(node.dataset.task);
    if(!first||typeof node.animate!=='function')return;
    const last=node.getBoundingClientRect();
    const dx=first.left-last.left,dy=first.top-last.top;
    if(Math.abs(dx)<1&&Math.abs(dy)<1)return;
    node.animate([{transform:`translate(${dx}px,${dy}px)`},{transform:'none'}],{duration:340,easing:'cubic-bezier(.2,.8,.2,1)'});
  });
}

async function flush(){
  if(saving)return;
  saving=true;
  try{
    while(intents.size){
      const batch=new Map(intents);
      const assignments=Object.fromEntries(TASKS.map(task=>[task.key,tierOf(task)]));
      try{
        await settingsPage.connection.saveAssignments(assignments);
        for(const [key,tier] of batch)if(intents.get(key)===tier)intents.delete(key);
        if(!intents.size)ui.savedTick(el('routing-saved'),lastAction==='reset'?'已恢复推荐分工':'已保存');
      }catch(error){
        intents.clear();
        settingsPage.connection.reapplySavedAssignments();
        if(error?.name!=='AbortError')window.showToast?.(`模型分工没有保存：${error.message}`,'error');
      }
      render();
      settingsPage.connection.refreshUses();
    }
  }finally{saving=false}
}

function moveTask(key){
  const task=TASKS.find(item=>item.key===key);
  const select=task&&selectOf(task);
  if(!select||!loaded())return;
  const next=tierOf(task)==='fast'?'quality':'fast';
  intents.set(key,next);
  lastAction='move';
  flip(()=>{select.value=next;render()});
  document.querySelector(`.task-chip[data-task="${key}"]`)?.focus({preventScroll:true});
  settingsPage.connection.refreshUses();
  void flush();
}

function resetRecommended(){
  if(!loaded())return;
  lastAction='reset';
  flip(()=>{
    TASKS.forEach(task=>{
      if(tierOf(task)===task.rec)return;
      intents.set(task.key,task.rec);
      const select=selectOf(task);
      if(select)select.value=task.rec;
    });
    render();
  });
  document.querySelector('.task-chip[data-task]')?.focus({preventScroll:true});
  settingsPage.connection.refreshUses();
  void flush();
}
el('routing-reset')?.addEventListener('click',resetRecommended);

/* 读回设置后，把还没保存完的换道意图重新套上 */
function afterApply(){
  for(const [key,tier] of intents){
    const task=TASKS.find(item=>item.key===key);
    const select=task&&selectOf(task);
    if(select)select.value=tier;
  }
  render();
}

function status(){
  if(!loaded())return {tone:'',text:''};
  const custom=TASKS.filter(task=>tierOf(task)!==task.rec).length;
  return {tone:'',text:custom?`${custom} 项与推荐不同`:'按推荐分工'};
}

settingsPage.routing=Object.freeze({mount:render,render,afterApply,status});
})();
