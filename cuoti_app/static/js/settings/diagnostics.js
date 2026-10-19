(function initSettingsDiagnostics(){
'use strict';
/* ③ AI 使用情况与 ⑦ 运行状态：只读的统计和本机体检。打开本页各读一次，点刷新或“重新诊断”才重读。 */
const settingsPage=window.Cuoti.settings;
const settingsAPI=settingsPage.api,settingsRender=settingsPage.render,ui=settingsPage.ui;
const {h,term}=ui;
const {fmt,pct,kTokens,labelOf,POLICY_LABELS,JOB_LABELS,HEALTH_CHECKS,HEALTH_STATUS_LABELS}=settingsRender;
const el=id=>document.getElementById(id);
const num=value=>{const number=Number(value);return Number.isFinite(number)?number:0};
const listOf=value=>(Array.isArray(value)?value.filter(row=>row&&typeof row==='object'):[]);

/* ── ③ AI 使用情况 ── */
const ROW_LIMIT=6;
let usage=null,usageError='',usageAt=null,usageView='jobs',expanded=false;
const rateOf=row=>(row.success_rate==null?null:Math.max(0,Math.min(1,num(row.success_rate))));
/* 需要留意：成功率偏低、截断多次或重试占比偏高 */
const needsWatch=row=>{
  const rate=rateOf(row);
  return (rate!=null&&rate<.9)||num(row.truncations)>=2||num(row.retries)/Math.max(1,num(row.count))>=.08;
};
const total=(rows,pick)=>rows.reduce((sum,row)=>sum+num(pick(row)),0);

function summary(){
  const tasks=listOf(usage?.tasks),jobs=listOf(usage?.business_tasks);
  const rated=jobs.filter(row=>rateOf(row)!=null);
  const ratedCount=total(rated,row=>row.count);
  return {tasks,jobs,jobCount:total(jobs,row=>row.count),
    jobRate:ratedCount?rated.reduce((sum,row)=>sum+num(row.count)*rateOf(row),0)/ratedCount:null,
    failed:total(jobs,row=>row.failed),calls:total(tasks,row=>row.count),
    retries:total(tasks,row=>row.retries),truncations:total(tasks,row=>row.truncations),
    watch:tasks.filter(needsWatch)};
}

function kpi(label,value,unit,note,watch=false){
  return h('div',{class:`usage-kpi${watch?' is-watch':''}`},h('span',{text:label}),
    h('strong',null,value,unit?h('small',{text:unit}):null),h('em',{text:note,title:note}));
}
function meter(rate){
  if(rate==null)return h('span',{class:'usage-meter',text:'—'});
  return h('div',{class:'usage-meter',title:`成功率 ${pct(rate,1)}`},
    h('div',{class:`usage-meter-track${rate<.95?' is-low':''}`},h('i',{style:`width:${(rate*100).toFixed(1)}%`})),
    h('b',{text:pct(rate)}));
}
const flags=items=>h('div',{class:'usage-flags'},...items.filter(Boolean).map(([text,quiet])=>h('span',{class:`usage-flag${quiet?' is-quiet':''}`,text})));

function jobRow(row){
  const count=num(row.count),attempts=num(row.attempts);
  return h('div',{class:'usage-row'},
    h('span',{class:'usage-row-name',text:labelOf(JOB_LABELS,row.task)}),meter(rateOf(row)),
    h('span',{class:'usage-count',text:`${fmt(count)} 个`}),
    flags([num(row.failed)>0&&[`失败 ${fmt(row.failed)}`],num(row.cancelled)>0&&[`取消 ${fmt(row.cancelled)}`,true],
      count>0&&attempts/count>=1.05&&[`平均尝试 ${(attempts/count).toFixed(1)} 次`,true]]));
}
function callRow(row,maxP95){
  const watch=needsWatch(row),rate=rateOf(row);
  const p50=num(row.p50_tokens),p95=num(row.p95_tokens);
  return h('div',{class:'usage-row'},
    h('span',{class:'usage-row-name',text:labelOf(POLICY_LABELS,row.task)}),
    h('div',{class:'usage-range',title:`一半调用不超过 ${fmt(p50)} Token，约 95% 不超过 ${fmt(p95)} Token`},
      h('div',{class:'usage-range-track'},
        h('i',{class:'p95',style:`width:${Math.min(100,p95/maxP95*100).toFixed(1)}%`}),
        h('i',{class:'p50',style:`width:${Math.min(100,p50/maxP95*100).toFixed(1)}%`})),
      h('small',{text:`${kTokens(p50)} / ${kTokens(p95)}`})),
    h('span',{class:'usage-count',text:`${fmt(row.count)} 次`}),
    flags([rate!=null&&rate<.95&&[`成功 ${pct(rate)}`],num(row.retries)>0&&[`重试 ${fmt(row.retries)}`,!watch],
      num(row.truncations)>0&&[`截断 ${fmt(row.truncations)}`]]));
}

function renderUsageMeta(){
  const meta=el('usage-meta');
  if(!meta)return;
  if(usageError&&usage){meta.textContent='刷新失败，显示上次结果';meta.title=usageError;return}
  meta.title='';
  meta.textContent=usage?(num(usage.event_count)?`最近 ${fmt(usage.event_count)} 条记录 · ${settingsRender.clockLabel(usageAt||new Date())} 读取`:'暂无记录'):'';
}

function renderUsage(){
  const kpis=el('usage-kpis'),list=el('usage-rows'),hint=el('usage-view-hint');
  ui.setSeg(el('usage-view'),usageView);
  hint?.replaceChildren(...(usageView==='jobs'
    ?['你发起的任务最终有没有拿到结果']
    :['每次向 AI 发出的请求 · 深色为一般用量（',term('P50','一半调用的用量不超过这个值'),'），浅色为偏高时（',
      term('P95','约 95% 的调用不超过这个值；偏高说明少数请求特别长'),'）']));
  renderUsageMeta();
  if(!usage){
    kpis?.replaceChildren();
    list?.replaceChildren(h('p',{class:'set-loading',text:usageError?`使用情况没有读到（${usageError}），点右上角刷新重试`:'正在读取…'}));
    return;
  }
  const s=summary();
  kpis?.replaceChildren(
    kpi('任务成功率',s.jobRate==null?'—':pct(s.jobRate,1),'',s.jobCount?`${fmt(s.jobCount)} 个任务 · 失败 ${fmt(s.failed)}`:'还没有任务记录'),
    kpi('AI 调用',s.calls?fmt(s.calls):'—',s.calls?'次':'',s.calls?`重试 ${fmt(s.retries)} · 截断 ${fmt(s.truncations)}`:'还没有调用记录'),
    kpi('需要留意',s.calls?(s.watch.length?String(s.watch.length):'无'):'—',s.watch.length?'类':'',
      s.watch.length?s.watch.map(row=>labelOf(POLICY_LABELS,row.task)).join('、'):s.calls?'重试和截断都在正常范围':'—',s.watch.length>0));
  if(!list)return;
  if(!s.calls&&!s.jobCount){
    list.replaceChildren(h('div',{class:'usage-empty'},ui.iconNode('gauge'),h('strong',{text:'还没有 AI 使用记录'}),
      h('span',{text:'录入题目、分析错题或组卷之后，这里会显示成功率和用量。'})));
    return;
  }
  const source=usageView==='jobs'
    ?[...s.jobs].sort((a,b)=>num(b.count)-num(a.count))
    :[...s.tasks].sort((a,b)=>Number(needsWatch(b))-Number(needsWatch(a))||num(b.count)-num(a.count));
  if(!source.length){
    list.replaceChildren(h('p',{class:'set-loading',text:usageView==='jobs'?'这段时间没有任务结果记录，切到“按 AI 调用”看看。':'这段时间没有 AI 调用记录。'}));
    return;
  }
  const maxP95=Math.max(1,...s.tasks.map(row=>num(row.p95_tokens)));
  const visible=expanded?source:source.slice(0,ROW_LIMIT);
  list.replaceChildren(...visible.map(row=>(usageView==='jobs'?jobRow(row):callRow(row,maxP95))));
  if(source.length>ROW_LIMIT){
    list.append(h('button',{type:'button',class:'set-text-btn usage-more','aria-expanded':String(expanded),onclick:()=>{
      expanded=!expanded;renderUsage();
      el('usage-rows')?.querySelector('.usage-more')?.focus({preventScroll:true});
    }},ui.iconNode('chevronDown'),expanded?'收起':`显示全部 ${source.length} 类`));
  }
}

async function loadAIUsage({force=false}={}){
  const operation=settingsAPI.beginOperation('loadAIUsage');
  const button=el('ai-usage-refresh');
  if(force)ui.spinning(button,true);
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.aiUsage);
    if(!operation.current())return;
    usage=payload&&typeof payload==='object'?payload:{};
    usageError='';usageAt=new Date();
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    usageError=error.message||'未知错误';
  }finally{
    if(operation.current()){ui.spinning(button,false);renderUsage();ui.changed()}
  }
}

function usageStatus(){
  if(!usage)return {tone:usageError?'error':'',text:usageError?'读取失败':''};
  const s=summary();
  if(!s.jobCount&&!s.calls)return {tone:'',text:'暂无记录'};
  if(s.jobRate==null)return {tone:'',text:`${fmt(s.calls)} 次调用`};
  return {tone:'',text:`成功率 ${pct(s.jobRate,1)}${s.watch.length?` · ${s.watch.length} 类留意`:''}`};
}

el('ai-usage-refresh')?.addEventListener('click',()=>loadAIUsage({force:true}));
ui.bindSeg(el('usage-view'),value=>{usageView=value==='calls'?'calls':'jobs';expanded=false;renderUsage()});

/* ── ⑦ 运行状态 ── */
let health=null,healthError='',healthLoaded=false,healthChecking=false;
const checksOf=()=>listOf(health?.checks);
const checkMeta=check=>{
  const id=String(check.check_id||'unknown');
  return HEALTH_CHECKS[id]||{label:`未知检查（${id}）`,icon:'circleHelp',advice:''};
};

function healthStatus(){
  if(!health){
    if(healthChecking)return {tone:'busy',text:'正在检查…'};
    return healthError?{tone:'error',text:'诊断失败'}:{tone:'',text:'尚未检查'};
  }
  const checks=checksOf();
  const fatal=checks.filter(check=>check.status==='fatal').length;
  const warn=checks.filter(check=>check.status!=='ok'&&check.status!=='fatal').length;
  if(fatal)return {tone:'error',text:`${fatal} 项失败`};
  if(warn)return {tone:'warn',text:`${warn} 项警告`};
  return {tone:'ok',text:checks.length?'全部正常':'暂无检查项'};
}

function renderHealth(){
  ui.setState(el('runtime-health-state'),healthStatus());
  const checks=checksOf();
  el('health-tiles')?.replaceChildren(...checks.map((check,index)=>{
    const meta=checkMeta(check),status=String(check.status||'unknown');
    const message=String(check.message||'未提供诊断信息');
    const tone=status==='ok'?'':status==='fatal'?' is-fatal':status==='degraded'?' is-degraded':' is-unknown';
    return h('div',{class:`health-tile${tone}`,role:'listitem',style:`--i:${index}`,title:message,
      'aria-label':`${meta.label}：${HEALTH_STATUS_LABELS[status]||'状态未知'}，${message}`},
    ui.iconNode(meta.icon),meta.label,h('i',{'aria-hidden':'true'}));
  }));
  el('health-issues')?.replaceChildren(...checks.filter(check=>check.status!=='ok').map(check=>{
    const meta=checkMeta(check),fatal=check.status==='fatal';
    return h('div',{class:`health-issue${fatal?' is-fatal':''}`},ui.iconNode(fatal?'circleX':'triangleAlert'),
      h('div',null,h('strong',{text:`${meta.label}：${String(check.message||'未提供诊断信息')}`}),meta.advice?h('small',{text:meta.advice}):null));
  }));
}

async function loadRuntimeHealth({force=false,silent=false}={}){
  if(healthLoaded&&!force)return;
  const operation=settingsAPI.beginOperation('loadRuntimeHealth');
  const tiles=el('health-tiles');
  const restore=force&&!silent?ui.busyStart(el('runtime-health-refresh'),'诊断中…'):null;
  if(restore&&tiles&&!ui.prefersReduced()){tiles.classList.toggle('is-scanning',false);void tiles.offsetWidth;tiles.classList.toggle('is-scanning',true)}
  healthChecking=true;
  if(!health)renderHealth();
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.health(force));
    if(!operation.current())return;
    if(String(payload?.contract_version)!=='1')throw new Error('诊断契约版本不兼容');
    health=payload;healthError='';healthLoaded=true;
    const checked=settingsRender.parseTime(payload.checked_at);
    ui.setMessage(el('runtime-health-status'),`检查于 ${settingsRender.clockLabel(checked||new Date())}`);
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    healthError=error.message||'未知错误';
    if(!silent||!health)ui.setMessage(el('runtime-health-status'),`诊断失败：${healthError}`,'error');
  }finally{
    if(operation.current()){
      healthChecking=false;
      restore?.();
      tiles?.classList.toggle('is-scanning',false);
      renderHealth();ui.changed();
    }else restore?.();
  }
}

el('runtime-health-refresh')?.addEventListener('click',()=>loadRuntimeHealth({force:true}));
window.loadRuntimeHealth=loadRuntimeHealth;

function unmount(){
  healthLoaded=false;healthChecking=false;expanded=false;
  el('health-tiles')?.classList.toggle('is-scanning',false);
}

settingsPage.diagnostics=Object.freeze({loadAIUsage,loadRuntimeHealth,unmount,usageStatus,healthStatus});
})();
