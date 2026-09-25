const REMOTE_DRAFT_KEY='cuoti.remote-drafts.v1';
const REMOTE_DRAFT_SCHEMA=1;
const REMOTE_DRAFT_TTL=7*24*60*60*1000;
const REMOTE_DRAFT_PANELS=new Set(['p-add','p-input','p-exam']);
let remoteDraft={schema:REMOTE_DRAFT_SCHEMA,expires_at:Date.now()+REMOTE_DRAFT_TTL,fields:{},reasons:[]};

function readRemoteDraft(){
  try{
    const value=JSON.parse(sessionStorage.getItem(REMOTE_DRAFT_KEY)||'null');
    if(value?.schema!==REMOTE_DRAFT_SCHEMA||Number(value.expires_at)<=Date.now()){
      sessionStorage.removeItem(REMOTE_DRAFT_KEY);
      return;
    }
    if(value.fields&&typeof value.fields==='object')remoteDraft=value;
  }catch(error){
    try{sessionStorage.removeItem(REMOTE_DRAFT_KEY)}catch(ignore){}
  }
}

function isRemoteDraftField(element){
  const panel=element?.closest?.('.panel');
  return Boolean(
    element?.id&&panel&&REMOTE_DRAFT_PANELS.has(panel.id)&&
    !element.hasAttribute('data-remote-draft-ignore')&&
    ['INPUT','SELECT','TEXTAREA'].includes(element.tagName)&&
    !['file','password','hidden'].includes(String(element.type||'').toLowerCase())
  );
}

function writeRemoteDraft(){
  remoteDraft.schema=REMOTE_DRAFT_SCHEMA;
  remoteDraft.expires_at=Date.now()+REMOTE_DRAFT_TTL;
  try{sessionStorage.setItem(REMOTE_DRAFT_KEY,JSON.stringify(remoteDraft))}catch(error){}
}

function captureRemoteDraftField(element){
  if(!isRemoteDraftField(element))return;
  remoteDraft.fields[element.id]=['checkbox','radio'].includes(element.type)
    ?Boolean(element.checked):String(element.value||'');
  writeRemoteDraft();
}

function restoreRemoteDraftField(id){
  const element=document.getElementById(id);
  if(!isRemoteDraftField(element)||!Object.hasOwn(remoteDraft.fields,id))return false;
  const value=remoteDraft.fields[id];
  if(['checkbox','radio'].includes(element.type)){
    element.checked=Boolean(value);
    return true;
  }
  if(element.tagName==='SELECT'){
    if(![...element.options].some(option=>option.value===String(value)))return false;
  }
  element.value=String(value??'');
  element.dispatchEvent(new Event('input'));
  return true;
}

function saveRemoteDraft(){
  document.querySelectorAll('#p-add input,#p-add select,#p-add textarea,#p-input input,#p-input select,#p-input textarea,#p-exam input,#p-exam select,#p-exam textarea')
    .forEach(captureRemoteDraftField);
  remoteDraft.reasons=[...document.querySelectorAll('#p-add .tag.on')]
    .map(item=>String(item.dataset.r||'')).filter(Boolean);
  writeRemoteDraft();
}

function restoreRemoteDraft(){
  readRemoteDraft();
  Object.keys(remoteDraft.fields).forEach(restoreRemoteDraftField);
  const reasonSet=new Set(Array.isArray(remoteDraft.reasons)?remoteDraft.reasons:[]);
  document.querySelectorAll('#p-add .tag').forEach(tag=>{
    const active=reasonSet.has(String(tag.dataset.r||''));
    tag.classList.toggle('on',active);
    tag.setAttribute('aria-pressed',String(active));
  });
  selectedReasons=[...reasonSet];
}

function clearRemoteDraftPanel(panelId){
  const panel=document.getElementById(panelId);
  if(!panel||!REMOTE_DRAFT_PANELS.has(panelId))return;
  panel.querySelectorAll('input[id],select[id],textarea[id]').forEach(element=>{
    if(isRemoteDraftField(element))delete remoteDraft.fields[element.id];
  });
  if(panelId==='p-add')remoteDraft.reasons=[];
  writeRemoteDraft();
}

function updateVisualViewport(){
  const height=Math.round(window.visualViewport?.height||window.innerHeight);
  document.documentElement.style.setProperty('--app-height',height+'px');
}

document.addEventListener('input',event=>captureRemoteDraftField(event.target));
document.addEventListener('change',event=>captureRemoteDraftField(event.target));
document.addEventListener('focusin',event=>{
  if(!window.matchMedia('(pointer:coarse)').matches)return;
  if(!event.target.matches('input,textarea,select'))return;
  window.setTimeout(()=>event.target.scrollIntoView({block:'center',behavior:'smooth'}),180);
});
window.visualViewport?.addEventListener('resize',updateVisualViewport);
window.addEventListener('orientationchange',updateVisualViewport);
window.addEventListener('online',()=>showToast('网络已恢复，未提交草稿仍保留在本页','success'));
document.addEventListener('DOMContentLoaded',()=>{
  updateVisualViewport();
  restoreRemoteDraft();
},{once:true});

window.restoreRemoteDraftField=restoreRemoteDraftField;
window.saveRemoteDraft=saveRemoteDraft;
window.clearRemoteDraftPanel=clearRemoteDraftPanel;
