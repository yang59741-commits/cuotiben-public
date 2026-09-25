(function initExamHistory(){
'use strict';
const examPage=window.Cuoti.exam;

let examHistoryItems=[];

let examHistoryNextCursor=null;

let examHistoryTotal=0;

let examHistoryHasMore=false;

let examHistoryRequestRevision=0;

let examHistoryRequestController=null;

let examHistoryMode='active';

function formatExamHistoryDate(value){
  const full=String(value||'').trim();
  const match=full.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  return match?`${match[1]} ${match[2]}`:full;
}

async function loadExamHistory(options={}){
  const append=Boolean(options.append);
  if(append&&(!examHistoryHasMore||!examHistoryNextCursor))return;
  const mode=examHistoryMode;
  const revision=++examHistoryRequestRevision;
  if(examHistoryRequestController)examHistoryRequestController.abort();
  examHistoryRequestController=new AbortController();
  if(!append){examHistoryNextCursor=null;examHistoryHasMore=false}
  if(!append){examHistoryItems=[];examHistoryTotal=0}
  const div=document.getElementById('exam-history');
  const more=document.getElementById('exam-history-load-more');
  const total=document.getElementById('exam-history-total');
  if(!append){
    if(more)more.hidden=true;
    if(total)total.textContent='';
    div.textContent=mode==='trash'?'正在读取回收站...':'正在读取历史试卷...';
  }
  if(more)more.disabled=append;
  let page,data;
  try{
    const params=new URLSearchParams({limit:'30'});
    if(append&&examHistoryNextCursor)params.set('after',examHistoryNextCursor);
    const endpoint=mode==='trash'?'/api/v2/trash/exams?':'/api/v2/exams?';
    page=await examPage.api.request(endpoint+params.toString(),{signal:examHistoryRequestController.signal});
    if(revision!==examHistoryRequestRevision||mode!==examHistoryMode)return;
    const items=Array.isArray(page?.items)?page.items:[];
    examHistoryItems=append?[...examHistoryItems,...items]:items;
    examHistoryNextCursor=page?.next_cursor||null;
    examHistoryHasMore=Boolean(page?.has_more&&examHistoryNextCursor);
    examHistoryTotal=Math.max(0,safeInteger(page?.total));
    data=examHistoryItems;
  }catch(error){
    if(error?.name==='AbortError'||revision!==examHistoryRequestRevision||mode!==examHistoryMode)return;
    div.textContent=(mode==='trash'?'回收站':'历史试卷')+'读取失败：'+error.message;
    return;
  }finally{
    if(revision===examHistoryRequestRevision&&mode===examHistoryMode&&more)more.disabled=false;
  }
  if(!Array.isArray(data))data=[];
  if(more)more.hidden=!examHistoryHasMore;
  if(total)total.textContent=examHistoryTotal?`已显示 ${data.length} / ${examHistoryTotal} 份`:'';
  if(!data.length){
    div.innerHTML=mode==='trash'
      ?emptyStateHTML('回收站为空',{icon:'archive',compact:true})
      :emptyStateHTML('暂无历史试卷',{icon:'history',description:'生成的试卷会保存在这里。',compact:true});
    return;
  }
  div.innerHTML=data.map(exam=>{
    const id=safeInteger(exam.id);
    if(mode==='trash'){
      return `<div class="exam-history-item is-trash"><span class="exam-history-title" title="${escapeHTML(exam.title||'')}">${escapeHTML(exam.title||'')}</span><span class="exam-history-time" title="${escapeHTML(exam.deleted_at||'')}">${escapeHTML(formatExamHistoryDate(exam.deleted_at))}</span><button type="button" class="btn btn-out btn-xs js-restore-exam" data-id="${id}" title="恢复试卷" aria-label="恢复试卷">${icon('undo')}</button></div>`;
    }
    return `<div class="exam-history-item"><button type="button" class="exam-history-title js-view-exam" data-id="${id}" title="${escapeHTML(exam.title||'')}">${escapeHTML(exam.title||'')}</button><span class="exam-history-time" title="${escapeHTML(exam.created_at||'')}">${escapeHTML(formatExamHistoryDate(exam.created_at))}</span><button type="button" class="btn btn-d btn-xs js-delete-exam" data-id="${id}" title="移入回收站" aria-label="移入回收站">${icon('archive')}</button></div>`;
  }).join('');
  div.querySelectorAll('.js-view-exam').forEach(item=>{
    item.addEventListener('click',()=>examPage.preview.viewExam(safeInteger(item.dataset.id)));
  });
  div.querySelectorAll('.js-delete-exam').forEach(button=>{
    button.addEventListener('click',()=>delExam(safeInteger(button.dataset.id)));
  });
  div.querySelectorAll('.js-restore-exam').forEach(button=>{
    button.addEventListener('click',()=>restoreExam(safeInteger(button.dataset.id),button));
  });
}

function switchExamHistoryMode(mode){
  if(!['active','trash'].includes(mode)||mode===examHistoryMode)return;
  examHistoryMode=mode;
  if(examHistoryRequestController)examHistoryRequestController.abort();
  examHistoryRequestController=null;
  examHistoryRequestRevision+=1;
  examHistoryItems=[];examHistoryNextCursor=null;examHistoryTotal=0;examHistoryHasMore=false;
  document.querySelectorAll('.js-exam-history-tab').forEach(button=>{
    const selected=button.dataset.historyMode===mode;
    button.classList.toggle('on',selected);
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  });
  const more=document.getElementById('exam-history-load-more');
  const total=document.getElementById('exam-history-total');
  if(more){more.hidden=true;more.disabled=false}
  if(total)total.textContent='';
  examPage.preview.hideExamPreview();
  void loadExamHistory();
}

async function delExam(id){
  showModal('移入回收站', '确定将这份试卷移入回收站吗？之后可以从回收站恢复。', 'info',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>' +
    '<button class="btn btn-d btn-sm" id="modal-confirm-btn">移入回收站</button>');
  document.getElementById('modal-confirm-btn').onclick = async function(){
    hideModal();
    try{
      await examPage.api.request('/api/v2/exams/'+id,{method:'DELETE'});
      if(examPage.state.currentExamPreviewId===id)examPage.preview.hideExamPreview();
      await loadExamHistory();
    }catch(error){
    if(error?.name==='AbortError')return;
      showModal('移入回收站失败',error.message,'error');
    }
  };
}

async function restoreExam(id,button){
  if(!id||button?.disabled)return;
  if(button)button.disabled=true;
  try{
    await examPage.api.request(`/api/v2/trash/exams/${id}/restore`,{
      method:'POST',body:JSON.stringify({})
    });
    showToast('试卷已恢复','success');
    await loadExamHistory();
  }catch(error){
    if(error?.name==='AbortError')return;
    showModal('恢复试卷失败',error.message,'error');
  }finally{
    if(button?.isConnected)button.disabled=false;
  }
}

let binding=null;
function bind(){
  if(binding)return;
  binding=new AbortController();
document.getElementById('exam-history-load-more')?.addEventListener('click',()=>loadExamHistory({append:true}),{signal:binding.signal});

document.querySelectorAll('.js-exam-history-tab').forEach(button=>{
  button.addEventListener('click',()=>switchExamHistoryMode(button.dataset.historyMode),{signal:binding.signal});
});

document.querySelector('.exam-history-tabs')?.addEventListener('keydown',event=>{
  const tabs=[...document.querySelectorAll('.js-exam-history-tab')];
  const index=tabs.indexOf(document.activeElement);
  if(index<0)return;
  let next=index;
  if(event.key==='ArrowRight')next=(index+1)%tabs.length;
  else if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;
  else if(event.key==='Home')next=0;
  else if(event.key==='End')next=tabs.length-1;
  else return;
  event.preventDefault();
  tabs[next].focus();
  switchExamHistoryMode(tabs[next].dataset.historyMode);
},{signal:binding.signal});
}
function unbind(){
  binding?.abort();binding=null;
  examHistoryRequestRevision+=1;
  examHistoryRequestController?.abort();
}

examPage.history=Object.freeze({
  formatExamHistoryDate,
  loadExamHistory,
  switchExamHistoryMode,
  delExam,
  restoreExam,
  bind,
  unbind
});
})();
