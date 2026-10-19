(function initSelectionFree(){
'use strict';
const examPage=window.Cuoti.exam;
const availableExamChapterIds=new Set();

let examChapterRequestRevision=0;

let examChapterRequestController=null;

let examBankCoverageRequestRevision=0;

let examBankCoverageRequestController=null;

function isExcludedExamChapter(chapter){
  const name=String(chapter?.name??'').replace(/\s+/g,'');
  return name.includes('场论初步');
}

function resetExamBankCoverage(message='选择科目后显示有效题库覆盖情况',state='idle'){
  if(examBankCoverageRequestController)examBankCoverageRequestController.abort();
  examBankCoverageRequestRevision+=1;
  const card=document.getElementById('exam-bank-coverage');
  if(!card)return;
  card.className='exam-bank-coverage is-'+state;
  card.replaceChildren();
  const text=document.createElement('span');
  text.className='exam-bank-coverage-message';
  text.textContent=message;
  card.append(text);
}

function renderExamBankCoverage(payload){
  const card=document.getElementById('exam-bank-coverage');
  if(!card)return;
  const status=String(payload?.status??'ready').toLowerCase();
  const errors=Array.isArray(payload?.errors)
    ?payload.errors.map(error=>String(error?.message??error?.error??error??'').trim()).filter(Boolean)
    :payload?.errors?[String(payload.errors)]:[];
  const total=Math.max(0,safeInteger(
    payload?.total_questions??payload?.valid_question_count??payload?.valid_count
  ));
  const duplicate=Math.max(0,safeInteger(
    payload?.duplicate_count??payload?.duplicate_question_count
  ));
  const excluded=Math.max(0,safeInteger(
    payload?.excluded_count??payload?.excluded_question_count
  ));
  const chapters=Array.isArray(payload?.chapters)?payload.chapters:[];
  const chapterCount=chapters.length||examPage.state.selectedExamChapters.size||availableExamChapterIds.size;
  const snapshot=String(payload?.snapshot_id??payload?.snapshot??'').trim();
  const isError=errors.length>0||['error','failed','blocked','invalid'].includes(status);
  const isPending=['building','pending','updating','loading'].includes(status);
  card.className='exam-bank-coverage '+(isError?'is-error':isPending?'is-loading':'is-ready');

  if(isError){
    const detail=errors.length?errors.join('；'):'有效题库索引不可用，请检查题库文件';
    card.innerHTML=`
      <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
      <span class="exam-bank-coverage-message"><strong>题库检查未通过</strong>${escapeHTML(detail)}</span>`;
    return;
  }
  if(isPending){
    card.innerHTML=`
      <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
      <span class="exam-bank-coverage-message"><strong>题库索引正在更新</strong>完成后会自动用于组卷</span>`;
    return;
  }

  const scope=examPage.state.selectedExamChapters.size?'当前所选章节':'当前科目';
  const snapshotLabel=snapshot
    ?`索引 ${escapeHTML(snapshot.length>12?snapshot.slice(0,12)+'…':snapshot)}`
    :'索引已校验';
  card.innerHTML=`
    <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
    <span class="exam-bank-coverage-message">
      <strong>${scope}可用 ${total} 题</strong>
      覆盖 ${chapterCount} 个章节 · 排除 ${excluded} 题 · 去重 ${duplicate} 题
    </span>
    <span class="exam-bank-coverage-snapshot" title="${escapeHTML(snapshot||'有效题库索引已校验')}">${snapshotLabel}</span>`;
}

async function loadExamBankCoverage(){
  const subjectSelect=document.getElementById('e-subject');
  const sid=subjectSelect?.value||'';
  if(!sid){
    resetExamBankCoverage();
    return;
  }
  const revision=++examBankCoverageRequestRevision;
  if(examBankCoverageRequestController)examBankCoverageRequestController.abort();
  examBankCoverageRequestController=new AbortController();
  const card=document.getElementById('exam-bank-coverage');
  if(card){
    card.className='exam-bank-coverage is-loading';
    card.innerHTML='<span class="exam-bank-coverage-dot" aria-hidden="true"></span><span class="exam-bank-coverage-message">正在核对有效题库...</span>';
  }
  const chapterIds=examPage.state.selectedExamChapters.size
    ?[...examPage.state.selectedExamChapters]
    :[...availableExamChapterIds];
  chapterIds.sort((first,second)=>first-second);
  const params=new URLSearchParams({
    subject_id:sid,
    chapter_ids:chapterIds.join(',')
  });
  try{
    const payload=await examPage.api.request(
      '/api/v2/question-bank/coverage?'+params.toString(),
      {signal:examBankCoverageRequestController.signal}
    );
    if(
      revision!==examBankCoverageRequestRevision||
      subjectSelect.value!==sid
    )return;
    renderExamBankCoverage(payload);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examBankCoverageRequestRevision)return;
    resetExamBankCoverage('题库覆盖读取失败：'+error.message,'error');
  }
}

async function loadExamChapters(){
  const sid=document.getElementById('e-subject').value;
  const div=document.getElementById('e-chapters');
  examPage.state.selectedExamChapters.clear();
  availableExamChapterIds.clear();
  resetExamBankCoverage(sid?'正在读取有效题库范围...':'选择科目后显示有效题库覆盖情况',sid?'loading':'idle');
  const revision=++examChapterRequestRevision;
  if(examChapterRequestController)examChapterRequestController.abort();
  div.replaceChildren();
  if(!sid){
    div.textContent='请先选择科目';
    return;
  }
  examChapterRequestController=new AbortController();
  div.setAttribute('aria-busy','true');
  div.textContent='正在读取章节...';
  try{
    const [chs,subjectCoverage]=await Promise.all([
      examPage.api.request(
        '/api/chapters?'+new URLSearchParams({subject_id:sid}).toString(),
        {signal:examChapterRequestController.signal}
      ),
      examPage.api.request(
        '/api/v2/question-bank/coverage?'+new URLSearchParams({subject_id:sid}).toString(),
        {signal:examChapterRequestController.signal}
      )
    ]);
    if(
      revision!==examChapterRequestRevision||
      document.getElementById('e-subject').value!==sid
    )return;
    const coveredChapterIds=new Set(
      (Array.isArray(subjectCoverage?.chapters)?subjectCoverage.chapters:[])
        .filter(chapter=>safeInteger(chapter?.total_questions)>0)
        .map(chapter=>safeInteger(chapter?.chapter_id))
        .filter(Boolean)
    );
    const availableChapters=Array.isArray(chs)
      ?chs.filter(chapter=>
        !isExcludedExamChapter(chapter)&&
        coveredChapterIds.has(safeInteger(chapter.id))
      )
      :[];
    if(!availableChapters.length){
      div.textContent='该科目暂无可用于组卷的章节';
      resetExamBankCoverage('该科目没有通过有效范围校验的题目','error');
      return;
    }
    availableChapters.forEach(chapter=>{
      const id=safeInteger(chapter.id);
      if(id)availableExamChapterIds.add(id);
    });
    div.innerHTML='<div class="chip-wrap">'+availableChapters.map(chapter=>{
      const id=safeInteger(chapter.id);
      return `<button type="button" class="chip js-exam-chapter" data-id="${id}" aria-pressed="false">${escapeHTML(chapter.name||'')}</button>`;
    }).join('')+'</div>';
    div.querySelectorAll('.js-exam-chapter').forEach(chip=>{
      chip.addEventListener('click',()=>pickChapter(chip,safeInteger(chip.dataset.id)));
    });
    renderExamBankCoverage(subjectCoverage);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examChapterRequestRevision)return;
    div.textContent='章节读取失败：'+error.message;
    resetExamBankCoverage('题库范围读取失败：'+error.message,'error');
  }finally{
    if(revision===examChapterRequestRevision)div.removeAttribute('aria-busy');
  }
}

function pickChapter(el,id){
  if(!id)return;
  el.classList.toggle('on');
  el.setAttribute('aria-pressed',String(el.classList.contains('on')));
  if(el.classList.contains('on'))examPage.state.selectedExamChapters.add(id);
  else examPage.state.selectedExamChapters.delete(id);
  void loadExamBankCoverage();
}

let examSubjectRequestRevision=0;
async function loadExamSubjects(){
  const sel=document.getElementById('e-subject');
  if(!sel||sel.dataset.subjectLoading==='1')return;
  if(sel.options.length>1){
    if(document.getElementById('e-chapters')?.getAttribute('aria-busy')==='true'){
      await loadExamChapters();
    }else if(document.getElementById('exam-bank-coverage')?.classList.contains('is-loading')){
      await loadExamBankCoverage();
    }
    return;
  }
  const revision=++examSubjectRequestRevision;
  sel.dataset.subjectLoading='1';
  sel.replaceChildren(new Option('正在读取科目...',''));
  sel.disabled=true;
  sel.setAttribute('aria-busy','true');
  syncCustomSelect(sel);
  try{
    const data=await examPage.api.request('/api/subjects');
    if(!Array.isArray(data)||!data.length){
      sel.replaceChildren(new Option('暂无可选科目',''));
      syncCustomSelect(sel);
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
    sel.onchange=()=>loadExamChapters();
    await loadExamChapters();
  }catch(error){
    if(error?.name==='AbortError')return;
    console.error('loadSubjects error:',error);
    sel.replaceChildren(new Option('科目读取失败',''));
    sel.disabled=true;
    syncCustomSelect(sel);
    showModal('读取失败',error.message,'error');
  }finally{
    if(revision===examSubjectRequestRevision){
      delete sel.dataset.subjectLoading;
      sel.removeAttribute('aria-busy');
    }
  }
}


function cancelReads(){
  examSubjectRequestRevision+=1;examChapterRequestRevision+=1;examBankCoverageRequestRevision+=1;
  examChapterRequestController?.abort();examBankCoverageRequestController?.abort();
  const subject=document.getElementById('e-subject');
  if(subject){delete subject.dataset.subjectLoading;subject.removeAttribute('aria-busy');}
}

examPage.selectionFree=Object.freeze({loadExamSubjects,isExcludedExamChapter,resetExamBankCoverage,renderExamBankCoverage,loadExamBankCoverage,loadExamChapters,pickChapter,cancelReads});
})();
