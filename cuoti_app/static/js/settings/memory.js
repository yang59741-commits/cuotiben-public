(function initSettingsMemory(){
'use strict';
/* ④ AI 记忆：三个记忆盒，学习开关即改即存；“⋯”里放查看画像、重建摘要和清空。
   画像管理抽屉第一次打开时才创建，挂在 body 下，沿用后台任务面板的抽屉样式；打开即读取。 */
const settingsPage=window.Cuoti.settings;
const settingsState=settingsPage.state,settingsAPI=settingsPage.api,settingsRender=settingsPage.render,ui=settingsPage.ui;
const {h}=ui,{fmt}=settingsRender;
const LABELS=settingsRender.MEMORY_LABELS;
const ORDER=Object.freeze(['free','mistakes','past']);
const el=id=>document.getElementById(id);
const count=value=>{const number=Number(value);return Number.isFinite(number)&&number>0?Math.floor(number):0};
let memory=null,loadError='';
const saving=new Set();

function spaces(){
  const rows=Array.isArray(memory?.namespaces)?memory.namespaces:[];
  return ORDER.map(type=>rows.find(row=>row&&row.memory_type===type)).filter(Boolean);
}
const spaceOf=type=>spaces().find(row=>row.memory_type===type);
const switchOf=type=>document.querySelector(`.mem-space[data-type="${type}"] .set-switch`);
const memoryMessage=(text,tone)=>ui.setMessage(el('ai-memory-status'),text,tone);

/* ── 记忆盒 ── */
function spaceSwitch(space){
  const type=space.memory_type;
  const button=h('button',{type:'button',class:'set-switch',role:'switch','aria-checked':String(!space.paused),
    'aria-label':`${LABELS[type]}：自动学习`,'aria-busy':saving.has(type)?'true':null},
  h('span',{class:'set-switch-track'},h('span')),
  h('span',{class:'set-switch-text'},h('strong',{text:space.paused?'已暂停':'学习中'})));
  ui.bindSwitch(button,learn=>setPaused(type,!learn));
  return button;
}

function spaceMenu(trigger,type){
  const space=spaceOf(type);
  if(!space)return;
  const events=count(space.event_count),profiles=count(space.profile_count);
  ui.toggleMenu(trigger,node=>node.append(
    ui.menuItem({icon:'layoutGrid',label:'查看画像',hint:profiles?`${fmt(profiles)} 条`:'还没有画像',onClick:()=>openDrawer(type,trigger)}),
    ui.menuItem({icon:'refresh',label:'重建摘要',hint:'按已有事件重新整理画像',disabled:!events,onClick:()=>rebuild(type)}),
    h('hr'),
    ui.menuItem({icon:'trash',label:'清空这一类',hint:'永久删除，另外两类不受影响',danger:true,disabled:!events&&!profiles,onClick:()=>clearSpace(type)})
  ),{label:`${LABELS[type]}：更多操作`});
}

function spaceBox(space){
  const type=space.memory_type,label=LABELS[type];
  const events=count(space.event_count);
  const meta=space.paused?'暂停期间不记新事件，已有记忆照常参考'
    :events?`${fmt(events)} 个事件 · 已确认 ${fmt(count(space.confirmed_count))}`:'还没有记录';
  return h('div',{class:`mem-space${space.paused?' is-paused':''}`,dataset:{type}},
    h('div',{class:'mem-space-head'},h('span',{text:label}),
      h('span',{class:'set-anchor'},h('button',{type:'button',class:'set-icon-btn','aria-haspopup':'menu','aria-expanded':'false',
        'aria-label':`${label}：更多操作`,title:'更多操作',html:ui.iconHTML('more'),onclick:event=>spaceMenu(event.currentTarget,type)}))),
    h('div',{class:'mem-space-figure'},fmt(count(space.profile_count)),h('small',{text:'条画像'})),
    h('div',{class:'mem-space-meta',text:meta}),
    spaceSwitch(space));
}

function render(){
  const root=el('mem-spaces'),rows=spaces();
  if(root){
    if(!memory)root.replaceChildren(h('p',{class:'set-loading',text:loadError?'记忆状态没有读到，点右上角刷新重试':'正在读取…'}));
    else if(!rows.length)root.replaceChildren(h('p',{class:'set-loading',text:'暂时没有记忆数据'}));
    else root.replaceChildren(...rows.map(spaceBox));
  }
  const meta=el('mem-meta');
  if(meta){
    const events=rows.reduce((sum,row)=>sum+count(row.event_count),0);
    const profiles=rows.reduce((sum,row)=>sum+count(row.profile_count),0);
    const cleanup=count(memory?.pending_cleanup_count);
    meta.textContent=memory?`共 ${fmt(events)} 个事件 · ${fmt(profiles)} 条画像${cleanup?` · ${fmt(cleanup)} 份已删除试卷的记忆待清理`:''}`:'';
  }
  ui.changed();
}

async function loadAIMemory({force=false}={}){
  const operation=settingsAPI.beginOperation('loadAIMemory');
  const button=el('ai-memory-refresh');
  if(force)ui.spinning(button,true);
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.aiMemory);
    if(!operation.current())return;
    memory=payload&&typeof payload==='object'?payload:{};
    if(loadError){loadError='';memoryMessage('')}
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    loadError=error.message||'未知错误';
    memoryMessage(`记忆状态读取失败：${loadError}`,'error');
  }finally{
    if(operation.current()){ui.spinning(button,false);render()}
  }
}

/* 学习开关：先在界面上切换，保存失败再切回去 */
async function setPaused(type,paused){
  const space=spaceOf(type);
  if(!space||saving.has(type))return;
  const operation=settingsAPI.beginOperation(`memoryPause:${type}`);
  saving.add(type);
  space.paused=paused;
  render();switchOf(type)?.focus({preventScroll:true});
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiMemoryPause,{method:'PUT',body:JSON.stringify({memory_type:type,paused})});
    if(!operation.current())return;
    const current=spaceOf(type);
    if(current)current.paused=typeof result?.paused==='boolean'?result.paused:paused;
    saving.delete(type);
    render();
    const control=switchOf(type);
    control?.focus({preventScroll:true});
    ui.savedTick(control,paused?'已暂停':'已恢复');
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    const current=spaceOf(type);
    if(current)current.paused=!paused;
    saving.delete(type);
    render();switchOf(type)?.focus({preventScroll:true});
    window.showToast?.(`“${LABELS[type]}”的学习开关没有保存：${error.message}`,'error');
  }
}

async function rebuild(type){
  const operation=settingsAPI.beginOperation('memoryCommand');
  memoryMessage(`正在重新整理“${LABELS[type]}”的画像…`);
  try{
    await settingsAPI.request(settingsAPI.paths.aiMemoryRebuild,{method:'POST',body:JSON.stringify({memory_type:type})});
    if(!operation.current())return;
    memoryMessage(`“${LABELS[type]}”的画像已重新整理`,'ok');
    await loadAIMemory();
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    memoryMessage(`重建没有完成：${error.message}`,'error');
  }
}

async function clearSpace(type){
  const space=spaceOf(type),label=LABELS[type];
  const operation=settingsAPI.beginOperation('memoryCommand');
  const ok=await ui.confirm({title:'清空 AI 记忆',type:'error',danger:true,confirmLabel:'清空',
    body:[`将永久清空“${label}”的 ${fmt(count(space?.event_count))} 个事件和 ${fmt(count(space?.profile_count))} 条画像，无法恢复。`,'错题、试卷和另外两类记忆不受影响。']});
  if(!ok||!operation.current())return;
  memoryMessage(`正在清空“${label}”…`);
  try{
    await settingsAPI.request(settingsAPI.paths.aiMemory,{method:'DELETE',body:JSON.stringify({memory_type:type,confirm:true})});
    if(!operation.current())return;
    memoryMessage(`已清空“${label}”记忆`,'ok');
    await loadAIMemory();
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    memoryMessage(`清空没有完成：${error.message}`,'error');
  }
}

/* ── 画像管理抽屉 ── */
const PAGE_SIZE=10;
/* 状态筛选与列表接口一一对应（单个 status），不显示各筛选的条数，免得为计数多发请求 */
const STATUS_FILTERS=Object.freeze([['','全部'],['recent','近期'],['stable','稳定'],['confirmed','已确认'],['rejected','已否定'],['suppressed','已隐藏']]);
const DONE_TEXT=Object.freeze({confirm:'画像已确认，之后会优先参考。',correct:'画像已纠正，之后按纠正后的内容参考。',
  reject:'画像已否定，不再参考。',suppress:'画像已隐藏，可在“已隐藏”里找到。'});
const drawer={scope:'free',status:'',type:'',page:1,last:1,total:0,loading:false,commandPending:false,returnFocus:null};
const drawerOpen=()=>Boolean(el('mem-drawer')&&!el('mem-drawer').hidden);
const modalOpen=()=>Boolean(document.getElementById('modal')&&!document.getElementById('modal').classList.contains('hidden'));
const drawerMessage=(text,tone)=>ui.setMessage(el('mem-drawer-msg'),text,tone);

function buildDrawer(){
  if(el('mem-drawer'))return;
  const scope=h('div',{class:'set-seg set-seg-fill',id:'mem-scope',role:'radiogroup','aria-label':'记忆范围'},
    ...ORDER.map(type=>h('button',{type:'button',role:'radio','aria-checked':'false',dataset:{value:type},text:LABELS[type]})));
  const chips=h('div',{class:'mem-chips',id:'mem-status-filter',role:'radiogroup','aria-label':'画像状态'},
    ...STATUS_FILTERS.map(([value,label])=>h('button',{type:'button',class:'mem-chip',role:'radio','aria-checked':'false',dataset:{value},text:label})));
  const typeSelect=h('select',{id:'mem-entry-type',class:'sel-sm','aria-label':'画像类型'},h('option',{value:'',text:'全部类型'}),
    ...Object.entries(settingsRender.AI_PROFILE_TYPE_LABELS).map(([value,label])=>h('option',{value,text:label})));
  const panel=h('aside',{class:'task-panel set-drawer',id:'mem-drawer',role:'dialog','aria-modal':'true','aria-labelledby':'mem-drawer-title',tabindex:'-1',hidden:true},
    h('div',{class:'task-panel-header'},h('h2',{id:'mem-drawer-title',text:'画像管理'}),
      h('button',{type:'button',class:'icon-btn',id:'mem-drawer-close','aria-label':'关闭画像管理',title:'关闭',html:ui.iconHTML('x'),onclick:closeDrawer})),
    h('div',{class:'mem-drawer-tools'},scope,chips,
      h('label',{class:'mem-type'},h('span',{text:'类型'}),typeSelect),
      h('p',{class:'set-hint',text:'画像会自动形成并供组卷参考，确认是可选的。确认或纠正过的画像会被优先参考；否定、隐藏的不再参考。'})),
    h('p',{class:'set-msg mem-drawer-msg',id:'mem-drawer-msg',role:'status','aria-live':'polite'}),
    h('div',{class:'mem-list',id:'mem-list'}),
    h('nav',{class:'mem-pager','aria-label':'画像分页'},
      h('button',{type:'button',class:'btn btn-out btn-xs',id:'mem-prev',text:'上一页',onclick:()=>loadProfile(drawer.page-1)}),
      h('span',{id:'mem-page','aria-live':'polite'}),
      h('button',{type:'button',class:'btn btn-out btn-xs',id:'mem-next',text:'下一页',onclick:()=>loadProfile(drawer.page+1)})));
  const scrim=h('button',{type:'button',class:'task-panel-scrim',id:'mem-scrim','aria-label':'关闭画像管理',tabindex:'-1',hidden:true,onclick:closeDrawer});
  document.body.append(panel,scrim);
  ui.bindSeg(scope,value=>{drawer.scope=value;drawerMessage('');void loadProfile(1)});
  ui.bindSeg(chips,value=>{drawer.status=value;drawerMessage('');void loadProfile(1)});
  typeSelect.addEventListener('change',()=>{drawer.type=typeSelect.value;drawerMessage('');void loadProfile(1)});
}

function syncFilters(){
  ui.setSeg(el('mem-scope'),drawer.scope);
  ui.setSeg(el('mem-status-filter'),drawer.status);
  const select=el('mem-entry-type');
  if(select){select.value=drawer.type;window.Cuoti.ui.selects?.sync?.(select)}
}

function syncPager(){
  const busy=drawer.loading||drawer.commandPending;
  const label=el('mem-page');
  if(label)label.textContent=drawer.total?`第 ${drawer.page} / ${drawer.last} 页 · 共 ${fmt(drawer.total)} 条`:'';
  const prev=el('mem-prev'),next=el('mem-next');
  if(prev){prev.hidden=drawer.last<=1;prev.disabled=busy||drawer.page<=1}
  if(next){next.hidden=drawer.last<=1;next.disabled=busy||drawer.page>=drawer.last}
}

function safeError(error,verb){
  if(error?.status===404)return '这条画像已不存在，请刷新列表。';
  if(error?.status===400)return verb==='读取'?'画像筛选条件无效。':'画像操作无效，请刷新后重试。';
  if(error?.status===503)return '画像服务暂时不可用，请稍后重试。';
  return `${verb}画像失败，请稍后重试。`;
}

function entryMenu(trigger,key,status,value){
  const hidden=status==='rejected'||status==='suppressed';
  ui.toggleMenu(trigger,node=>node.append(...[
    hidden&&ui.menuItem({icon:'circleCheck',label:'确认准确',hint:'重新开始参考这条',onClick:()=>command(key,'confirm')}),
    ui.menuItem({icon:'edit',label:'纠正…',hint:'改成准确的说法',onClick:()=>correct(key,value)}),
    status!=='rejected'&&ui.menuItem({icon:'circleX',label:'否定…',hint:'这条不对，不再参考',onClick:()=>confirmCommand(key,'reject')}),
    status!=='suppressed'&&ui.menuItem({icon:'eyeOff',label:'隐藏…',hint:'不再参考，仍可在“已隐藏”里找到',onClick:()=>confirmCommand(key,'suppress')})
  ].filter(Boolean)),{label:'画像操作'});
}

function entryNode(item){
  if(!item||typeof item!=='object')return null;
  const key=String(item.key??'').trim();
  if(!key)return null;
  const status=String(item.status||'');
  const corrected=typeof item.correction==='string'&&item.correction.length>0;
  const value=String((corrected?item.effective_value:(item.effective_value??item.value))??'').trim();
  const confidence=Math.round(Math.max(0,Math.min(1,Number(item.confidence)||0))*100);
  const confirmed=status==='confirmed',hidden=status==='rejected'||status==='suppressed';
  return h('article',{class:`mem-entry${confirmed?' is-confirmed':''}`,dataset:{key}},
    h('div',{class:'mem-entry-head'},h('span',{class:'mem-entry-type',text:settingsRender.AI_PROFILE_TYPE_LABELS[item.entry_type]||'画像'}),h('strong',{text:value||'（空值）'})),
    h('div',{class:'mem-entry-meta'},
      h('span',{class:`mem-entry-status s-${status}`,text:settingsRender.AI_PROFILE_STATUS_LABELS[status]||'状态未知'}),
      corrected?h('span',{class:'mem-entry-corrected',text:'已按你的纠正'}):null,
      h('span',{class:'mem-confidence',title:'置信度：AI 对这条画像有多大把握'},h('i',null,h('b',{style:`width:${confidence}%`})),`${confidence}%`),
      h('span',{text:`证据 ${fmt(count(item.evidence_count))} 条`}),
      h('span',{class:'mem-entry-actions'},
        !confirmed&&!hidden?h('button',{type:'button',class:'btn btn-out btn-xs',text:'确认准确',onclick:()=>command(key,'confirm')}):null,
        h('span',{class:'set-anchor'},h('button',{type:'button',class:'set-icon-btn','aria-haspopup':'menu','aria-expanded':'false',
          'aria-label':`“${value||'空值'}”的更多操作`,title:'更多操作',html:ui.iconHTML('more'),onclick:event=>entryMenu(event.currentTarget,key,status,value)})))));
}

async function loadProfile(page=1){
  const operation=settingsAPI.beginOperation('loadAIMemoryProfile');
  const list=el('mem-list');
  if(!list)return false;
  const requested=Math.max(1,Math.floor(Number(page))||1);
  drawer.loading=true;
  syncFilters();syncPager();
  list.setAttribute('aria-busy','true');
  list.replaceChildren(h('p',{class:'mem-loading',text:'正在读取画像…'}));
  try{
    const payload=await settingsAPI.aiMemoryProfilePage({memoryType:drawer.scope,page:requested,pageSize:PAGE_SIZE,status:drawer.status,entryType:drawer.type});
    if(!operation.current())return false;
    if(!payload||typeof payload!=='object'||!Array.isArray(payload.items))throw new Error('INVALID_PROFILE_PAGE');
    const total=count(payload.total);
    const pageSize=Math.min(100,count(payload.page_size)||PAGE_SIZE);
    const last=Math.max(1,Math.ceil(total/pageSize));
    const current=count(payload.page)||requested;
    if(total&&current>last)return loadProfile(last);
    Object.assign(drawer,{page:current,last,total});
    const nodes=payload.items.slice(0,pageSize).map(entryNode).filter(Boolean);
    list.replaceChildren(...(nodes.length?nodes:[h('p',{class:'mem-loading',text:drawer.status||drawer.type
      ?'没有符合条件的画像，换个筛选试试。':`“${LABELS[drawer.scope]}”还没有画像。分析成功或试卷发布后会自动积累。`})]));
    list.scrollTop=0;
    return true;
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return false;
    Object.assign(drawer,{last:1,total:0});
    list.replaceChildren(h('p',{class:'mem-loading is-error',text:safeError(error,'读取')}));
    return false;
  }finally{
    if(operation.current()){drawer.loading=false;list.removeAttribute('aria-busy');syncPager()}
  }
}

/* 列表重排后把焦点放回同一条画像；它已不在当前筛选里时回到抽屉本身 */
function focusEntry(key){
  const entry=[...document.querySelectorAll('#mem-list .mem-entry')].find(node=>node.dataset.key===key);
  (entry?.querySelector('.set-icon-btn')||el('mem-drawer'))?.focus({preventScroll:true});
}

async function command(key,action,value=null){
  if(drawer.commandPending||!drawerOpen())return false;
  const operation=settingsAPI.beginOperation('commandAIMemoryProfile');
  drawer.commandPending=true;
  syncPager();
  document.querySelectorAll('#mem-list button').forEach(button=>{button.disabled=true});
  drawerMessage('正在保存…');
  try{
    await settingsAPI.aiMemoryProfileCommand({memory_type:drawer.scope,key,action,value});
    if(!operation.current())return false;
    drawer.commandPending=false;
    const refreshed=await loadProfile(drawer.page);
    if(!settingsState.active)return false;
    drawerMessage(refreshed?DONE_TEXT[action]:`${DONE_TEXT[action]}列表没有刷新，请换个筛选再看。`,refreshed?'ok':'error');
    focusEntry(key);
    void loadAIMemory();
    return true;
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return false;
    drawerMessage(safeError(error,'操作'),'error');
    document.querySelectorAll('#mem-list button').forEach(button=>{button.disabled=false});
    focusEntry(key);
    return false;
  }finally{
    drawer.commandPending=false;
    if(settingsState.active)syncPager();
  }
}

async function correct(key,value){
  const text=await ui.prompt({title:'纠正画像',body:'写下你认为准确的说法。之后 AI 按纠正后的内容参考，旧推断不再同时进入上下文。',
    label:'纠正后的画像',value,maxLength:500,confirmLabel:'保存纠正',emptyMessage:'纠正内容不能为空'});
  if(text==null){focusEntry(key);return}
  await command(key,'correct',text);
}

/* 否定、隐藏之后仍可在对应筛选里“确认准确”找回，所以只做普通确认 */
async function confirmCommand(key,action){
  const reject=action==='reject';
  const ok=await ui.confirm({title:reject?'否定画像':'隐藏画像',type:'warning',confirmLabel:reject?'确认否定':'确认隐藏',
    body:reject?'否定后，这条画像不会继续进入后续 AI 上下文。':'隐藏后，这条画像不会继续进入后续 AI 上下文，但仍可在“已隐藏”筛选中查看。'});
  if(ok)await command(key,action);
  else focusEntry(key);
}

function showDrawer(open,{restore=true}={}){
  const panel=el('mem-drawer'),scrim=el('mem-scrim');
  if(!panel)return;
  const wasOpen=!panel.hidden;
  panel.hidden=!open;
  if(scrim)scrim.hidden=!open;
  if(open){
    ui.lockBackground('memory-drawer');
    if(!wasOpen)el('mem-drawer-close')?.focus();
    return;
  }
  if(!wasOpen)return;
  ui.closeMenu();
  ui.unlockBackground('memory-drawer');
  const target=drawer.returnFocus;
  drawer.returnFocus=null;
  if(restore)(target?.isConnected?target:el('ai-memory-profile-open'))?.focus({preventScroll:true});
}

function openDrawer(scope='free',trigger=document.activeElement){
  ui.closeMenu();
  buildDrawer();
  const wasOpen=drawerOpen();
  if(!wasOpen)drawer.returnFocus=trigger;
  drawer.scope=ORDER.includes(scope)?scope:'free';
  drawerMessage('');
  showDrawer(true);
  if(!wasOpen)window.pushHistoryLayer?.('settings-drawer',()=>showDrawer(false));
  void loadProfile(1);
}

function closeDrawer(){
  if(!drawerOpen())return;
  if(!window.dismissHistoryLayer?.('settings-drawer'))showDrawer(false);
}

function trapFocus(event){
  const panel=el('mem-drawer');
  const focusable=[...panel.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])')]
    .filter(node=>node.offsetParent!==null);
  if(!focusable.length){event.preventDefault();panel.focus();return}
  const first=focusable[0],last=focusable[focusable.length-1];
  if(!panel.contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first).focus()}
  else if(event.shiftKey&&(document.activeElement===first||document.activeElement===panel)){event.preventDefault();last.focus()}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
}
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||!drawerOpen()||modalOpen())return;
  if(event.key==='Escape'){event.preventDefault();closeDrawer();return}
  if(event.key==='Tab')trapFocus(event);
});

el('ai-memory-refresh')?.addEventListener('click',()=>loadAIMemory({force:true}));
el('ai-memory-profile-open')?.addEventListener('click',event=>{
  const first=spaces().find(row=>count(row.profile_count))?.memory_type||'free';
  openDrawer(first,event.currentTarget);
});

function status(){
  if(!memory)return {tone:loadError?'error':'',text:loadError?'读取失败':''};
  const rows=spaces();
  const profiles=rows.reduce((sum,row)=>sum+count(row.profile_count),0);
  const paused=rows.filter(row=>row.paused).length;
  return {tone:'',text:`${fmt(profiles)} 条画像${paused?` · ${paused} 类暂停`:''}`};
}

/* 离开页面：抽屉只收起界面，历史层沿用其他抽屉的做法留给返回键自然消化 */
function unmount(){
  saving.clear();
  drawer.loading=false;drawer.commandPending=false;
  if(drawerOpen())showDrawer(false,{restore:false});
}

settingsPage.memory=Object.freeze({loadAIMemory,loadAIMemoryProfile:loadProfile,openDrawer,closeDrawer,unmount,status});
})();
