let selectedReasons=[];

const ROUTE_PAGES=new Set(['add','list','input','review','exam','stats','settings']);
let mathRenderTicket=0;
const mathNodeStates=new WeakMap();
const mathRootPromises=new WeakMap();
const mathRootRequestMarkup=new WeakMap();
const mathRootTickets=new WeakMap();
let mathRenderQueue=Promise.resolve();
let modalRevision=0;
let modalReturnFocus=null;
let toastSequence=0;
window.navigationRevision=window.navigationRevision||0;
const TOAST_META=Object.freeze({
  info:{type:'info',title:'提示',icon:'circleHelp'},
  success:{type:'success',title:'已完成',icon:'check'},
  warning:{type:'warning',title:'请注意',icon:'lightbulb'},
  error:{type:'error',title:'操作失败',icon:'x'}
});
const MODAL_META=Object.freeze({
  info:{type:'info',icon:'circleHelp'},
  success:{type:'success',icon:'check'},
  warning:{type:'warning',icon:'lightbulb'},
  error:{type:'error',icon:'x'}
});
const historyLayers=[];
let ignoredLayerPopstates=0;

function markHistoryLayers(){
  document.documentElement.dataset.historyLayers=historyLayers.map(layer=>layer.name).join(',');
}

const ALLOWED_MODAL_TAGS=new Set([
  'p','br','strong','b','em','i','code','pre','ul','ol','li',
  'h3','h4','h5','blockquote','hr','div','span'
]);
const BLOCKED_MODAL_TAGS=new Set([
  'script','style','iframe','object','embed','link','meta','svg','math','form',
  'input','button','textarea','select'
]);

function mathRenderIsCurrent(target,ticket,options={}){
  if(mathRootTickets.get(target)!==ticket||!target?.isConnected)return false;
  const panel=target.matches?.('.panel')?target:target.closest?.('.panel');
  if(panel&&!panel.classList.contains('on'))return false;
  if(typeof options.isCurrent==='function'){
    try{
      if(!options.isCurrent())return false;
    }catch(error){
      return false;
    }
  }
  return true;
}

function collectMathNodes(root){
  const candidates=[];
  if(root.matches?.('.math,[data-math]'))candidates.push(root);
  root.querySelectorAll?.('.math,[data-math]').forEach(node=>candidates.push(node));
  if(!candidates.length)return [root];
  const unique=[...new Set(candidates)];
  // Keep outer math owners only, avoiding duplicate typesetting.
  return unique.filter(node=>!unique.some(other=>other!==node&&other.contains(node)));
}

function markMathNodeFailed(node,error){
  const state=mathNodeStates.get(node)||{};
  state.status='failed';
  state.error=String(error?.message||error||'公式渲染失败');
  state.renderedMarkup=null;
  mathNodeStates.set(node,state);
  if(node.dataset)node.dataset.mathState='failed';
  node.classList?.add('math-render-failed');
}

function markMathNodeRendered(node){
  const state=mathNodeStates.get(node)||{};
  state.status='rendered';
  state.error='';
  // Cache generated markup so unchanged nodes are not cleared and re-rendered.
  state.renderedMarkup=node.innerHTML;
  mathNodeStates.set(node,state);
  if(node.dataset)delete node.dataset.mathState;
  node.classList?.remove('math-render-failed');
}

function queueMathRender(task){
  const next=mathRenderQueue.then(task,task);
  mathRenderQueue=next.catch(()=>{});
  return next;
}

function renderMathNodes(root,options={}){
  options=options&&typeof options==='object'?options:{};
  const target=root||document.querySelector('.panel.on');
  if(!target||!target.isConnected)return Promise.resolve(false);
  const existing=mathRootPromises.get(target);
  const requestMarkup=target.innerHTML;
  if(existing&&!options.force&&mathRootRequestMarkup.get(target)===requestMarkup)return existing;
  const ticket=++mathRenderTicket;
  mathRootTickets.set(target,ticket);
  mathRootRequestMarkup.set(target,requestMarkup);
  let promise;
  promise=queueMathRender(async()=>{
    try{
      if(!mathRenderIsCurrent(target,ticket,options))return false;
      if(window.MathJax?.startup?.promise)await window.MathJax.startup.promise;
      if(!mathRenderIsCurrent(target,ticket,options))return false;
      const nodes=collectMathNodes(target);
      if(!window.MathJax?.typesetPromise){
        showMathUnavailable();
        nodes.forEach(node=>markMathNodeFailed(node,new Error('MathJax unavailable')));
        return false;
      }
      let allSucceeded=true;
      for(const node of nodes){
        if(!mathRenderIsCurrent(target,ticket,options))return false;
        if(!node.isConnected){allSucceeded=false;continue}
        const state=mathNodeStates.get(node);
        const currentMarkup=node.innerHTML;
        if(!options.force&&state?.status==='rendered'&&state.renderedMarkup===currentMarkup){
          continue;
        }
        try{
          const hasGeneratedOutput=Boolean(node.querySelector?.('mjx-container,.MathJax,.MathJax_Error,mjx-merror'));
          if(window.MathJax.typesetClear&&(state?.status==='rendered'||hasGeneratedOutput)){
            window.MathJax.typesetClear([node]);
          }
          await window.MathJax.typesetPromise([node]);
          if(!mathRenderIsCurrent(target,ticket,options)||!node.isConnected){
            allSucceeded=false;
            continue;
          }
          markMathNodeRendered(node);
        }catch(error){
          allSucceeded=false;
          markMathNodeFailed(node,error);
          console.warn('Math rendering skipped:',error?.message||error);
        }
      }
      return allSucceeded;
    }catch(error){
      console.warn('Math rendering skipped:',error?.message||error);
      return false;
    }finally{
      if(mathRootPromises.get(target)===promise){
        mathRootPromises.delete(target);
        mathRootRequestMarkup.delete(target);
      }
    }
  });
  mathRootPromises.set(target,promise);
  return promise;
}

async function renderMath(root,options={}){
  return renderMathNodes(root,options);
}

function showMathUnavailable(){
  const status=document.getElementById('math-status');
  if(status)status.hidden=false;
}

function escapeHTML(value){
  return window.Cuoti.ui.render.escape(value);
}

function emptyStateHTML(title,options={}){
  return window.Cuoti.ui.render.emptyState(title,options);
}

function safeInteger(value,fallback=0){
  const parsed=Number(value);
  return Number.isSafeInteger(parsed)?parsed:fallback;
}

function createCursorPager(limit=30){
  return {
    limit:Math.max(1,safeInteger(limit,30)),
    nextCursor:null,
    hasMore:false,
    total:0,
    reset(){this.nextCursor=null;this.hasMore=false;this.total=0},
    apply(page){
      this.nextCursor=page?.next_cursor==null?null:String(page.next_cursor);
      this.hasMore=Boolean(page?.has_more&&this.nextCursor);
      this.total=Math.max(0,safeInteger(page?.total));
      return Array.isArray(page?.items)?page.items:[];
    },
    params(append=false){
      const params=new URLSearchParams({limit:String(this.limit)});
      if(append&&this.nextCursor)params.set('after',this.nextCursor);
      return params;
    }
  };
}

function safeLocalURL(value){
  try{
    const url=new URL(String(value??''),window.location.origin);
    if(url.origin!==window.location.origin||!['http:','https:'].includes(url.protocol))return '';
    return url.pathname+url.search+url.hash;
  }catch(error){
    return '';
  }
}

function cleanProblemContent(value){
  return window.Cuoti.ui.render.cleanProblemContent(value);
}

function problemPreviewHTML(content,variant=''){
  return window.Cuoti.ui.render.problemPreview(content,variant);
}

function localDateInputValue(value=new Date()){
  const date=value instanceof Date?value:new Date(value);
  const pad=number=>String(number).padStart(2,'0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`;
}
function resetPageScroll(){
  const reset=()=>{
    const main=document.querySelector('.main');
    if(main)main.scrollTop=0;
    document.documentElement.scrollTop=0;
    document.body.scrollTop=0;
    window.scrollTo(0,0);
  };
  reset();
  window.requestAnimationFrame?.(reset);
  window.setTimeout(reset,0);
}

window.addEventListener('load',()=>{
  if(window.mathJaxLoadFailed||!window.MathJax?.typesetPromise)showMathUnavailable();
  void renderMath();
});
let lastLocalDate=localDateInputValue();
function syncLocalDateFields(){
  const today=localDateInputValue();
  const dateInput=document.getElementById('s-date');
  if(dateInput&&(!dateInput.value||dateInput.value===lastLocalDate))dateInput.value=today;
  const topDate=document.getElementById('top-date');
  if(topDate)topDate.textContent=new Date().toLocaleDateString('zh-CN',{weekday:'long',year:'numeric',month:'long',day:'numeric'});
  lastLocalDate=today;
}
syncLocalDateFields();
document.addEventListener('visibilitychange',()=>{if(!document.hidden)syncLocalDateFields()});
document.querySelectorAll('.tag').forEach(t=>{t.onclick=function(){
  this.classList.toggle('on');
  this.setAttribute('aria-pressed',String(this.classList.contains('on')));
  selectedReasons=[...document.querySelectorAll('.tag.on')].map(x=>x.dataset.r);
  window.saveRemoteDraft?.();
}});

async function api(url,opts={}){
  const requestOptions={...opts};
  const headers=new Headers(opts.headers||{});
  if(opts.body!==undefined&&!(opts.body instanceof FormData)&&!headers.has('Content-Type')){
    headers.set('Content-Type','application/json');
  }
  requestOptions.headers=headers;
  let response;
  try{
    response=await fetch(url,requestOptions);
  }catch(error){
    if(error?.name==='AbortError')throw error;
    throw new Error('无法连接到本地服务，请确认应用仍在运行。');
  }
  const contentType=String(response.headers.get('content-type')||'').toLowerCase();
  if(!contentType.includes('application/json')){
    throw new Error(`服务器返回了非 JSON 响应（HTTP ${response.status}）。`);
  }
  let data;
  try{
    data=await response.json();
  }catch(error){
    throw new Error(`服务器返回的 JSON 无法解析（HTTP ${response.status}）。`);
  }
  if(!response.ok){
    const message=typeof data?.error==='string'&&data.error.trim()
      ?data.error.trim()
      :(typeof data?.message==='string'&&data.message.trim()
        ?data.message.trim()
        :`请求失败（HTTP ${response.status}）。`);
    const requestError=new Error(message);
    requestError.status=response.status;
    requestError.data=data;
    throw requestError;
  }
  return data;
}

let reviewBadgeRevision=0;
async function refreshReviewBadge(){
  const revision=++reviewBadgeRevision;
  try{
    const reminder=await api('/api/v2/stats/reminders');
    if(revision!==reviewBadgeRevision)return;
    const count=Math.max(0,safeInteger(reminder?.due_count));
    const badge=document.getElementById('nav-review-count');
    if(badge){badge.textContent=count;badge.hidden=!count}
    return count;
  }catch(error){
    if(revision===reviewBadgeRevision){
      console.warn('Review badge could not be refreshed:',error.message);
    }
    return null;
  }
}
window.Cuoti.reviewBadge=Object.freeze({refresh:refreshReviewBadge});

function showToast(message,type='info',options={}){
  const region=document.getElementById('toast-region');
  if(!region)return null;
  const meta=TOAST_META[type]||TOAST_META.info;
  const toast=document.createElement('div');
  toast.className=`toast toast-${meta.type}`;
  toast.tabIndex=0;
  toast.dataset.toastId=String(++toastSequence);
  toast.setAttribute('role',meta.type==='error'?'alert':'status');
  const iconSlot=document.createElement('span');
  iconSlot.className='toast-icon';
  iconSlot.innerHTML=icon(meta.icon);
  const content=document.createElement('span');
  content.className='toast-content';
  const title=document.createElement('strong');
  title.className='toast-title';
  title.textContent=meta.title;
  const text=document.createElement('span');
  text.className='toast-message';
  text.textContent=String(message??'');
  content.append(title,text);
  const close=document.createElement('button');
  close.type='button';
  close.className='toast-close';
  close.setAttribute('aria-label','关闭提示');
  close.innerHTML=icon('x');
  toast.append(iconSlot,content,close);
  region.appendChild(toast);
  let remaining=Math.max(1500,Number(options.duration)||4500);
  let started=Date.now();
  let timer=null;
  const dismiss=()=>{if(timer)clearTimeout(timer);toast.remove()};
  const resume=()=>{
    if(options.persistent||!toast.isConnected)return;
    started=Date.now();
    timer=window.setTimeout(dismiss,remaining);
  };
  const pause=()=>{
    if(!timer)return;
    clearTimeout(timer);timer=null;
    remaining=Math.max(500,remaining-(Date.now()-started));
  };
  close.addEventListener('click',dismiss);
  toast.addEventListener('mouseenter',pause);
  toast.addEventListener('mouseleave',resume);
  toast.addEventListener('focusin',pause);
  toast.addEventListener('focusout',resume);
  resume();
  return toast;
}

function parseHashRoute(){
  const match=String(window.location.hash||'').match(/^#\/([^?]*)(?:\?(.*))?$/);
  if(!match||!ROUTE_PAGES.has(match[1]))return null;
  return {page:match[1],params:new URLSearchParams(match[2]||'')};
}

function routeURL(page,params){
  const url=new URL(window.location.href);
  const query=params instanceof URLSearchParams?params.toString():'';
  url.hash='#/'+page+(query?'?'+query:'');
  return url.pathname+url.search+url.hash;
}

function pushHistoryLayer(name,close){
  if(historyLayers.some(layer=>layer.name===name))return;
  window.history.pushState({...window.history.state,layer:name},'',window.location.href);
  historyLayers.push({name,close});
  markHistoryLayers();
}

function dismissHistoryLayer(name,afterClose){
  const layer=historyLayers.at(-1);
  if(!layer||layer.name!==name)return false;
  historyLayers.pop();
  markHistoryLayers();
  layer.close(false);
  ignoredLayerPopstates+=1;
  if(typeof afterClose==='function')window.addEventListener('popstate',afterClose,{once:true});
  window.history.back();
  return true;
}

window.addEventListener('popstate',()=>{
  if(ignoredLayerPopstates){ignoredLayerPopstates-=1;return}
  const layer=historyLayers.pop();
  markHistoryLayers();
  if(layer)layer.close(true);
});

function activatePage(n,params=new URLSearchParams()){
  window.navigationRevision+=1;
  const previousPage=document.querySelector('.panel.on')?.id?.slice(2);
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  const panel=document.getElementById('p-'+n);
  if(!panel)return;
  panel.classList.add('on');
  if(previousPage!==n){
    resetPageScroll();
  }
  document.querySelectorAll('.sb nav .nav-btn[data-page]').forEach(button=>{
    const active=button.dataset.page===n;
    button.classList.toggle('ac',active);
    button.setAttribute('aria-current',active?'page':'false');
  });
  const titles={
    add:['plus','标记错题'],
    list:['listChecks','错题列表'],
    stats:['chart','统计分析'],
    review:['refresh','复习'],
    input:['filePlus','录入题目'],
    exam:['fileText','组卷'],
    settings:['settings','AI 设置']
  };
  const title=titles[n];
  document.getElementById('page-title').innerHTML=title?icon(title[0])+`<span>${title[1]}</span>`:'';
  void window.Cuoti.features.activate(n,{params,navigationRevision:window.navigationRevision});
}

function applyHashRoute(){
  let route=parseHashRoute();
  if(!route){
    window.history.replaceState({route:'add'},'',routeURL('add'));
    route={page:'add',params:new URLSearchParams()};
  }
  activatePage(route.page,route.params);
}

function nav(n){
  const page=ROUTE_PAGES.has(n)?n:'add';
  let params=new URLSearchParams();
  if(page==='list'&&window.getListRouteParams)params=window.getListRouteParams();
  const target=routeURL(page,params);
  const current=window.location.pathname+window.location.search+window.location.hash;
  if(target===current){applyHashRoute();return}
  window.location.hash=target.slice(target.indexOf('#'));
}

async function invokeFeature(name,method,...args){
  try{
    const feature=await window.Cuoti.features.load(name);
    if(typeof feature[method]!=='function')throw new Error(`${name} 不支持 ${method}`);
    return await feature[method](...args);
  }catch(error){
    showToast(`功能暂时不可用：${error.message}`,'error',{persistent:true});
    return undefined;
  }
}

const UI_ACTIONS=Object.freeze({
  navigate:control=>nav(control.dataset.page),
  'export-data':()=>void invokeFeature('data','exportData'),
  'pick-import':()=>void invokeFeature('data','pickImport'),
  'add-mistake':()=>void invokeFeature('add','add'),
  'load-stats':()=>void invokeFeature('stats','refresh'),
  'open-spaced-review':control=>{
    const params=new URLSearchParams({mode:'spaced'});
    if(control.dataset.subjectId)params.set('subject_id',control.dataset.subjectId);
    if(control.dataset.chapterId)params.set('chapter_id',control.dataset.chapterId);
    const target=routeURL('review',params);
    window.location.hash=target.slice(target.indexOf('#'));
  },
  'pick-problem-file':()=>void invokeFeature('input','pickFile'),
  'submit-problem':()=>void invokeFeature('input','submit'),
  'switch-exam-mode':control=>void invokeFeature('exam','switchMode',control.dataset.examMode),
  'select-exam-mistakes':()=>void invokeFeature('exam','selectAll'),
  'clear-exam-mistakes':()=>void invokeFeature('exam','selectNone'),
  'generate-exam':()=>void invokeFeature('exam','generate'),
  'hide-modal':()=>hideModal()
});

document.addEventListener('click',event=>{
  const control=event.target.closest('[data-action]');
  if(!control)return;
  const action=UI_ACTIONS[control.dataset.action];
  if(!action)return;
  event.preventDefault();
  action(control,event);
});

window.addEventListener('hashchange',applyHashRoute);
document.addEventListener('DOMContentLoaded',applyHashRoute,{once:true});
document.addEventListener('DOMContentLoaded',()=>void refreshReviewBadge(),{once:true});

const chapterRequestState=new Map();

function syncCustomSelect(select){
  window.Cuoti.ui.selects?.sync(select);
}

function resetChapterSelect(prefix,message='请先选择科目'){
  const select=document.getElementById(prefix+'-chapter');
  if(!select)return;
  const current=chapterRequestState.get(prefix);
  current?.controller?.abort();
  chapterRequestState.set(prefix,{revision:(current?.revision||0)+1,controller:null});
  select.replaceChildren(new Option(message,''));
  select.disabled=true;
  select.removeAttribute('aria-busy');
  syncCustomSelect(select);
}

async function loadSubjects(prefix){
  const sel=document.getElementById(prefix+'-subject');
  if(!sel||sel.options.length>1||sel.dataset.subjectLoading==='1')return;
  sel.dataset.subjectLoading='1';
  sel.replaceChildren(new Option('正在读取科目...',''));
  sel.disabled=true;
  sel.setAttribute('aria-busy','true');
  syncCustomSelect(sel);
  try{
    const data=await api('/api/subjects');
    if(!Array.isArray(data)||!data.length){
      sel.replaceChildren(new Option('暂无可选科目',''));
      syncCustomSelect(sel);
      if(prefix!=='e')resetChapterSelect(prefix);
      return;
    }
    sel.replaceChildren(new Option('请选择',''));
    data.forEach(subject=>sel.add(new Option(
      String(subject.name??''),
      String(safeInteger(subject.id,''))
    )));
    sel.disabled=false;
    window.restoreRemoteDraftField?.(sel.id);
    syncCustomSelect(sel);
    if(prefix==='e'){sel.onchange=function(){loadExamChapters()}}
    else{sel.onchange=function(){loadChapters(prefix,this.value)}}
    if(sel.value){
      if(prefix==='e')await loadExamChapters();
      else await loadChapters(prefix,sel.value,{load:false});
    }else if(prefix==='e')await loadExamChapters();
    else resetChapterSelect(prefix);
  }catch(error){
    console.error('loadSubjects error:',error);
    sel.replaceChildren(new Option('科目读取失败',''));
    sel.disabled=true;
    syncCustomSelect(sel);
    showModal('读取失败',error.message,'error');
  }finally{
    delete sel.dataset.subjectLoading;
    sel.removeAttribute('aria-busy');
  }
}

async function loadChapters(prefix,sid,options={}){
  const sel=document.getElementById(prefix+'-chapter');
  if(!sel)return;
  const subject=document.getElementById(prefix+'-subject');
  const subjectId=String(sid??subject?.value??'').trim();
  const previous=chapterRequestState.get(prefix);
  previous?.controller?.abort();
  const revision=(previous?.revision||0)+1;
  if(!subjectId){
    chapterRequestState.set(prefix,{revision,controller:null});
    resetChapterSelect(prefix);
    return [];
  }
  const controller=new AbortController();
  chapterRequestState.set(prefix,{revision,controller});
  sel.replaceChildren(new Option('正在读取章节...',''));
  sel.disabled=true;
  sel.setAttribute('aria-busy','true');
  syncCustomSelect(sel);
  try{
    const chs=await api(
      '/api/chapters?'+new URLSearchParams({subject_id:subjectId}).toString(),
      {signal:controller.signal}
    );
    const current=chapterRequestState.get(prefix);
    if(current?.revision!==revision||(subject&&subject.value!==subjectId))return [];
    const chapters=Array.isArray(chs)?chs:[];
    if(!chapters.length&&prefix!=='i'){
      sel.replaceChildren(new Option('该科目暂无可选章节',''));
      sel.disabled=true;
      syncCustomSelect(sel);
      return [];
    }
    sel.replaceChildren(new Option(prefix==='f'?'全部章节':'请选择',''));
    if(prefix==='i')sel.add(new Option('综合题目','zonghe'));
    chapters.forEach(chapter=>sel.add(new Option(
      String(chapter.name??''),
      String(safeInteger(chapter.id,''))
    )));
    sel.disabled=false;
    window.restoreRemoteDraftField?.(sel.id);
    syncCustomSelect(sel);
    if(prefix==='f'&&options.load!==false)window.Cuoti.mistakes.controller.load();
    return chapters;
  }catch(error){
    if(error?.name==='AbortError'||chapterRequestState.get(prefix)?.revision!==revision)return [];
    console.error('loadChapters error:',error);
    sel.replaceChildren(new Option('章节读取失败',''));
    sel.disabled=true;
    syncCustomSelect(sel);
    showModal('读取失败',error.message,'error');
    return [];
  }finally{
    if(chapterRequestState.get(prefix)?.revision===revision)sel.removeAttribute('aria-busy');
  }
}

function closeModalDOM(){
  modalRevision+=1;
  const modal=document.getElementById('modal');
  if(modal.classList.contains('hidden'))return;
  modal.classList.add('hidden');
  document.querySelector('.main')?.removeAttribute('inert');
  document.querySelector('.sb')?.removeAttribute('inert');
  const restore=modalReturnFocus;
  modalReturnFocus=null;
  if(restore?.isConnected)restore.focus();
}

function hideModal(afterClose){
  if(!dismissHistoryLayer('modal',afterClose)){
    closeModalDOM();
    if(typeof afterClose==='function')afterClose();
  }
}

function showModal(title, msg, type, btnHTML){
  modalRevision+=1;
  const wasHidden=document.getElementById('modal').classList.contains('hidden');
  if(wasHidden){
    modalReturnFocus=document.activeElement;
    pushHistoryLayer('modal',()=>closeModalDOM());
  }
  document.querySelector('#modal .modal-box').classList.remove('modal-box-wide');
  document.getElementById('modal-title').textContent = title;
  const body=document.getElementById('modal-body');
  body.className='modal-body';
  body.textContent = msg;
  const meta=MODAL_META[type]||MODAL_META.info;
  document.getElementById('modal-header').className='modal-header '+meta.type;
  document.getElementById('modal-type-icon').innerHTML=icon(meta.icon);
  document.getElementById('modal-footer').innerHTML = btnHTML || '<button class="btn btn-p btn-sm" data-action="hide-modal">确定</button>';
  const modal=document.getElementById('modal');
  modal.classList.remove('hidden');
  document.querySelector('.main')?.setAttribute('inert','');
  document.querySelector('.sb')?.setAttribute('inert','');
  window.requestAnimationFrame(()=>{
    const preferred=document.getElementById('modal-confirm-btn')
      ||modal.querySelector('.modal-footer button')
      ||modal.querySelector('.modal-close')
      ||modal.querySelector('.modal-box');
    preferred?.focus();
  });
}

function sanitizeModalHTML(value){
  const source=protectModalMathHTML(String(value??'')
    .replace(/^\s*```(?:html)?\s*/i,'')
    .replace(/\s*```\s*$/,''));
  const parsed=new DOMParser().parseFromString(source,'text/html');
  const output=document.createElement('div');

  function appendSafe(node,parent){
    if(node.nodeType===3){
      parent.appendChild(document.createTextNode(node.textContent||''));
      return;
    }
    if(node.nodeType!==1)return;
    const tag=node.tagName.toLowerCase();
    if(BLOCKED_MODAL_TAGS.has(tag))return;
    const destination=ALLOWED_MODAL_TAGS.has(tag)?document.createElement(tag):parent;
    if(destination!==parent)parent.appendChild(destination);
    [...node.childNodes].forEach(child=>appendSafe(child,destination));
  }

  [...parsed.body.childNodes].forEach(node=>appendSafe(node,output));
  return output.innerHTML;
}

function protectModalMathHTML(value){
  return String(value??'').replace(
    /\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)/g,
    formula=>formula
      .replace(/&(?!(?:#\d+|#x[\da-f]+|[a-z][\da-z]*);)/gi,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
  );
}

function showRichModal(title, html, type, btnHTML){
  showModal(title,'',type,btnHTML);
  const token=modalRevision;
  document.querySelector('#modal .modal-box').classList.add('modal-box-wide');
  const body=document.getElementById('modal-body');
  body.classList.add('modal-rich','math');
  body.innerHTML=sanitizeModalHTML(html);
  void renderMathNodes(body,{isCurrent:()=>token===modalRevision});
}

function closeCurrentModalAfter(delay=0){
  const expectedRevision=modalRevision;
  window.setTimeout(()=>{
    if(expectedRevision===modalRevision)hideModal();
  },delay);
}

function trapModalFocus(event){
  const modal=document.getElementById('modal');
  if(modal.classList.contains('hidden')||event.key!=='Tab')return;
  const focusable=[...modal.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])')].filter(el=>!el.hidden);
  if(!focusable.length){event.preventDefault();modal.querySelector('.modal-box')?.focus();return}
  const first=focusable[0],last=focusable[focusable.length-1];
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
}

document.getElementById('modal').addEventListener('click',event=>{if(event.target===event.currentTarget)hideModal()});
document.addEventListener('keydown', event => {
  if(event.key==='Escape'&&!document.getElementById('modal').classList.contains('hidden')){event.preventDefault();hideModal();return}
  trapModalFocus(event);
});

window.Cuoti=window.Cuoti||{};
window.Cuoti.core=Object.freeze({
http:api,errors:error=>String(error?.message||'请求失败'),modal:showModal,
toast:showToast,math:renderMathNodes,html:escapeHTML,url:safeLocalURL,
pager:createCursorPager,date:localDateInputValue,jobs:(...args)=>window.trackJob?.(...args)
});
