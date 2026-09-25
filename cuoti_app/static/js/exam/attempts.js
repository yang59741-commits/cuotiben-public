(function initExamAttempts(){
'use strict';
const examPage=window.Cuoti.exam;

let attemptRevision=0;

async function startExamAttempt(examId){
  const revision=++attemptRevision;
  try{
    const attempt=await examPage.api.request(`/api/v2/exams/${examId}/attempts`,{
      method:'POST',body:JSON.stringify({})
    });
    if(revision!==attemptRevision)return;
    showExamAttemptEditor(attempt,examId);
  }catch(error){
    if(error?.name==='AbortError'||revision!==attemptRevision)return;showModal('无法开始作答',error.message,'error')}
}

async function showExamAttempts(examId,options={}){
  const revision=++attemptRevision;
  const append=Boolean(options.append),after=String(options.after||'');
  try{
    const params=new URLSearchParams({limit:'20'});if(after)params.set('after',after);
    const payload=await examPage.api.request(`/api/v2/exams/${examId}/attempts?${params}`);
    if(revision!==attemptRevision)return;
    const items=Array.isArray(payload?.items)?payload.items:[];
    if(!append)showModal('作答记录',items.length?'选择一条记录查看。':'暂无作答记录','info','<button class="btn btn-out btn-sm" onclick="hideModal()">关闭</button>');
    const body=document.getElementById('modal-body');
    if(!body)return;
    body.querySelector('#exam-attempt-load-more')?.remove();
    items.forEach(attempt=>{
      const row=document.createElement('div');row.className='exam-attempt-history-row';
      const marker=document.createElement('span');marker.className='exam-attempt-marker';marker.setAttribute('aria-hidden','true');
      const button=document.createElement('button');button.className='btn btn-out btn-sm exam-attempt-open';
      button.textContent=`${attempt.status==='submitted'?'已提交':'草稿'} · ${attempt.total_score}/${attempt.max_score} · ${attempt.started_at}`;
      button.onclick=()=>showExamAttemptEditor(attempt,examId);
      const remove=document.createElement('button');remove.className='btn btn-d btn-sm';remove.textContent='删除';
      remove.setAttribute('aria-label',`删除 ${attempt.started_at} 的作答记录`);
      remove.onclick=()=>deleteExamAttempt(attempt,examId);
      row.append(marker,button,remove);body.append(row);
    });
    if(payload?.has_more&&payload?.next_cursor){
      const more=document.createElement('button');more.id='exam-attempt-load-more';
      more.className='btn btn-out btn-sm';more.textContent=`加载更多（已显示 ${body.querySelectorAll('.exam-attempt-history-row').length} / ${safeInteger(payload.total)}）`;
      more.onclick=()=>showExamAttempts(examId,{append:true,after:payload.next_cursor});
      body.append(more);
    }
  }catch(error){
    if(error?.name==='AbortError'||revision!==attemptRevision)return;showModal('作答记录读取失败',error.message,'error')}
}

async function deleteExamAttempt(attempt,examId){
  showModal('确认删除作答记录','确定要删除这条作答记录吗？已回流到错题本的题目会保留。','error',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-d btn-sm" id="modal-confirm-btn">确定删除</button>');
  document.getElementById('modal-confirm-btn').onclick=async()=>{
    try{
      await examPage.api.request(`/api/v2/exam-attempts/${attempt.public_id}`,{method:'DELETE'});
      await showExamAttempts(examId||attempt.exam_id);
    }catch(error){
    if(error?.name==='AbortError')return;showModal('删除作答记录失败',error.message,'error')}
  };
}

function showExamAttemptEditor(attempt,examId){
  const revision=++attemptRevision;
  const submitted=attempt.status==='submitted';
  showModal(submitted?'作答记录':'记录作答','本功能只记录手工判定、分数和批注，不存储任何答案。','info',
    '<button class="btn btn-d btn-sm" id="attempt-delete">删除记录</button><button class="btn btn-out btn-sm" onclick="hideModal()">关闭</button>'+
    (submitted?'':'<button class="btn btn-out btn-sm" id="attempt-save">保存草稿</button><button class="btn btn-s btn-sm" id="attempt-submit">提交作答</button>'));
  document.querySelector('#modal .modal-box')?.classList.add('modal-box-wide');
  const body=document.getElementById('modal-body');
  const list=document.createElement('div');list.className='exam-attempt-editor';
  (attempt.items||[]).forEach(item=>{
    const card=document.createElement('div');card.className='exam-attempt-item';card.dataset.publicId=item.public_id;
    const title=document.createElement('strong');title.textContent=`第 ${safeInteger(item.position)} 题`;
    const content=document.createElement('div');content.className='attempt-question-content math';content.innerHTML=examPage.preview.latexToHtml(String(item.display_content||item.content||''));
    const status=document.createElement('select');status.className='attempt-result';status.disabled=submitted;
    [['unanswered:unknown','未作答'],['correct:none','独立答对'],['correct:hint','借助提示答对'],['incorrect:none','错误']].forEach(([value,label])=>status.add(new Option(label,value)));
    const savedResult=item.result_status||'unanswered',savedAssistance=item.assistance||'unknown';
    const savedValue=`${savedResult}:${savedAssistance}`;
    if(savedResult==='correct'&&savedAssistance==='unknown')status.add(new Option('正确（未记录辅助）',savedValue));
    status.value=[...status.options].some(option=>option.value===savedValue)?savedValue:(savedResult==='incorrect'?'incorrect:none':'unanswered:unknown');
    const score=document.createElement('input');score.type='number';score.min='0';score.step='0.5';score.className='attempt-score';score.value=String(item.score||0);score.disabled=submitted;
    const comment=document.createElement('input');comment.type='text';comment.maxLength=5000;comment.className='attempt-comment';comment.placeholder='手工批注（可选）';comment.value=String(item.comment||'');comment.disabled=submitted;
    card.append(title,content,status,score,comment);
    if(submitted&&item.result_status==='incorrect'){
      const returnButton=document.createElement('button');returnButton.className='btn btn-out btn-sm';
      returnButton.textContent=item.returned_mistake_public_id?'已回流错题本':'回流错题本';returnButton.disabled=Boolean(item.returned_mistake_public_id);
      returnButton.onclick=()=>openAttemptReturnDialog(attempt,item,examId);
      card.append(returnButton);
    }
    list.append(card);
  });
  body.append(list);
  document.getElementById('attempt-delete').onclick=()=>deleteExamAttempt(attempt,examId||attempt.exam_id);
  void renderMath(body);
  if(submitted)return;
  const maxScore=document.createElement('input');maxScore.id='attempt-max-score';maxScore.type='number';maxScore.min='0';maxScore.step='0.5';maxScore.value=String(attempt.max_score||0);maxScore.placeholder='满分';
  const duration=document.createElement('input');duration.id='attempt-duration';duration.type='number';duration.min='0';duration.value=String(attempt.duration_seconds||0);duration.placeholder='用时（秒）';
  const note=document.createElement('textarea');note.id='attempt-note';note.maxLength=5000;note.placeholder='本次作答备注';note.value=String(attempt.note||'');
  body.append(maxScore,duration,note);
  const save=async submit=>{
    const items=[...list.querySelectorAll('.exam-attempt-item')].map(card=>{
      const [result_status,assistance]=card.querySelector('.attempt-result').value.split(':');
      return {public_id:card.dataset.publicId,result_status,assistance,
      score:Number(card.querySelector('.attempt-score').value)||0,
      comment:card.querySelector('.attempt-comment').value.trim()
      };
    });
    try{
      const updated=await examPage.api.request(`/api/v2/exam-attempts/${attempt.public_id}`,{
        method:'PATCH',body:JSON.stringify({items,max_score:Number(maxScore.value)||0,duration_seconds:Number(duration.value)||0,note:note.value.trim(),submit})
      });
      if(revision!==attemptRevision)return;
      showExamAttemptEditor(updated,examId||attempt.exam_id);
    }catch(error){
    if(error?.name==='AbortError'||revision!==attemptRevision)return;showModal('保存作答失败',error.message,'error')}
  };
  document.getElementById('attempt-save').onclick=()=>save(false);
  document.getElementById('attempt-submit').onclick=()=>save(true);
}

const EXAM_MISTAKE_REASON_OPTIONS=Object.freeze(['计算错误','概念不清','方法不会','卡在技巧','审题失误','时间不够']);

async function openAttemptReturnDialog(attempt,item,examId){
  const revision=++attemptRevision;
  const context=item.return_context||{},subjectId=safeInteger(context.subject_id);
  let chapters=[];
  try{if(subjectId)chapters=await examPage.api.request('/api/chapters?'+new URLSearchParams({subject_id:String(subjectId)}))}catch(_error){if(_error?.name==='AbortError')return;chapters=[]}
  if(revision!==attemptRevision)return;
  const preferred=safeInteger(context.default_chapter_id);
  const options=['<option value="">暂不归类</option>',...(Array.isArray(chapters)?chapters:[]).map(chapter=>`<option value="${safeInteger(chapter.id)}"${safeInteger(chapter.id)===preferred?' selected':''}>${escapeHTML(chapter.name||'')}</option>`)];
  const chapterHint=context.status==='multi'?'该题关联多个章节，请确认主章节。':context.status==='unknown'?'未找到可靠题位章节，可暂不归类。':'已按题位章节预选，可修改。';
  showModal('回流错题本','确认章节、错因和要保留的题级批注。','info',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-s btn-sm" id="attempt-return-confirm">确认回流</button>');
  const body=document.getElementById('modal-body');
  body.insertAdjacentHTML('beforeend',`<div class="form-row"><label>章节<select id="attempt-return-chapter">${options.join('')}</select></label><small>${escapeHTML(chapterHint)}</small></div><fieldset class="form-row"><legend>错因</legend><div class="tags">${EXAM_MISTAKE_REASON_OPTIONS.map(reason=>`<label class="tag"><input type="checkbox" value="${escapeHTML(reason)}">${escapeHTML(reason)}</label>`).join('')}</div></fieldset><label class="check-row"><input type="checkbox" id="attempt-return-copy-comment"${String(item.comment||'').trim()?' checked':''}>保留题级批注${String(item.comment||'').trim()?`：${escapeHTML(item.comment)}`:''}</label>`);
  document.getElementById('attempt-return-confirm').onclick=async()=>{
    const confirm=document.getElementById('attempt-return-confirm');confirm.disabled=true;
    try{
      const chapterValue=document.getElementById('attempt-return-chapter').value;
      const reasons=[...body.querySelectorAll('fieldset input:checked')].map(node=>node.value);
      await examPage.api.request(`/api/v2/exam-attempt-items/${item.public_id}/return-mistake`,{method:'POST',body:JSON.stringify({chapter_id:chapterValue?Number(chapterValue):null,reasons,copy_comment:document.getElementById('attempt-return-copy-comment').checked})});
      const refreshed=await examPage.api.request(`/api/v2/exam-attempts/${attempt.public_id}`);
      if(revision!==attemptRevision)return;
      showExamAttemptEditor(refreshed,examId||attempt.exam_id);
    }catch(error){
    if(error?.name==='AbortError')return;showModal('回流失败',error.message,'error')}
  };
}

examPage.attempts=Object.freeze({
  startExamAttempt,
  showExamAttempts,
  deleteExamAttempt,
  showExamAttemptEditor,
  openAttemptReturnDialog
});
})();
