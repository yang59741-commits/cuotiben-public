(function initSelectionMistakes(){
'use strict';
const examPage=window.Cuoti.exam;
const MAX_EXAM_MISTAKE_REFERENCES=20;
let examMistakes=[];

let examMistakeNextCursor=null;

let examMistakeTotal=0;

let examMistakeHasMore=false;

let examMistakeRequestRevision=0;

let examMistakeRequestController=null;

let examMistakeFiltersLoaded=false;

let examMistakeChapterRequestRevision=0;

let examMistakeChapterRequestController=null;

let examDailyRecommendations=[];

let examDailyRecommendationRevision=0;

let examDailyRecommendationController=null;

function resetExamMistakeCandidates(message='请先选择科目'){
  if(examMistakeRequestController)examMistakeRequestController.abort();
  examMistakeRequestRevision+=1;
  examMistakes=[];
  examDailyRecommendations=[];
  examDailyRecommendationRevision+=1;
  examDailyRecommendationController?.abort();
  const recommendationStatus=document.getElementById('exam-recommendation-status');
  if(recommendationStatus)recommendationStatus.textContent='选择科目后，待巩固推荐会排在列表前面。';
  examMistakeNextCursor=null;
  examMistakeTotal=0;
  examMistakeHasMore=false;
  examPage.state.selectedExamMistakeIds.clear();
  const div=document.getElementById('exam-mistake-list');
  if(div)div.innerHTML=emptyStateHTML('错题候选暂不可用',{icon:'circleAlert',description:message,compact:true});
  updateExamMistakeSelection();
}

function mergeExamMistakes(rows,{replace=false}={}){
  const byId=new Map((replace?examDailyRecommendations:examMistakes).map(item=>[safeInteger(item.id),item]));
  for(const item of rows)byId.set(safeInteger(item.id),item);
  examMistakes=[...byId.values()];
}

async function loadExamDailyRecommendations(){
  const revision=++examDailyRecommendationRevision;
  examDailyRecommendationController?.abort();
  const status=document.getElementById('exam-recommendation-status');
  const subjectId=document.getElementById('em-subject')?.value||'';
  const chapterId=document.getElementById('em-chapter')?.value||'';
  if(!subjectId){
    if(status)status.textContent='选择科目后，待巩固推荐会排在列表前面。';
    return;
  }
  examDailyRecommendationController=new AbortController();
  if(status)status.textContent='正在读取待巩固推荐…';
  const params=new URLSearchParams({limit:'10',subject_id:subjectId});
  if(chapterId)params.set('chapter_id',chapterId);
  try{
    const page=await examPage.api.request('/api/v2/daily-review/recommendations?'+params.toString(),{
      signal:examDailyRecommendationController.signal});
    if(revision!==examDailyRecommendationRevision)return;
    const seen=new Set();
    examDailyRecommendations=(Array.isArray(page?.items)?page.items:[]).filter(item=>{
      const id=safeInteger(item?.id);
      if(!id||seen.has(id))return false;
      seen.add(id);return true;
    });
    const loaded=new Set(examMistakes.map(item=>safeInteger(item.id)));
    mergeExamMistakes(examDailyRecommendations.filter(item=>!loaded.has(safeInteger(item.id))));
    if(status)status.textContent=examDailyRecommendations.length
      ?`${examDailyRecommendations.length} 道待巩固推荐已置顶 · 根据所选错题生成针对性新题`
      :'当前没有待巩固推荐，可从列表选择参考错题。';
    renderExamMistakes();
  }catch(error){
    if(error?.name==='AbortError'||revision!==examDailyRecommendationRevision)return;
    if(status)status.textContent='推荐暂不可用，仍可选择普通候选：'+error.message;
  }
}

async function prepareExamMistakeFilters(){
  const subjectSelect=document.getElementById('em-subject');
  if(!subjectSelect)return;
  if(!examMistakeFiltersLoaded){
    try{
      const subjects=await examPage.api.request('/api/subjects');
      subjectSelect.replaceChildren(new Option('请选择科目',''));
      subjects.forEach(subject=>subjectSelect.add(new Option(
        String(subject.name??''),
        String(safeInteger(subject.id,''))
      )));
      examMistakeFiltersLoaded=true;
    }catch(error){
    if(error?.name==='AbortError')return;
      resetExamMistakeCandidates('科目读取失败：'+error.message);
      return;
    }
  }
  if(subjectSelect.value)await loadExamMistakeChapters();
  else{
    resetExamMistakeCandidates();
    await loadExamDailyRecommendations();
  }
}

async function loadExamMistakeChapters(){
  const subjectSelect=document.getElementById('em-subject');
  const chapterSelect=document.getElementById('em-chapter');
  const sid=subjectSelect.value;
  const revision=++examMistakeChapterRequestRevision;
  if(examMistakeChapterRequestController)examMistakeChapterRequestController.abort();
  chapterSelect.replaceChildren(new Option(sid?'全部章节':'请先选择科目',''));
  resetExamMistakeCandidates(sid?'正在读取候选错题...':'请先选择科目');
  if(!sid)return;
  examMistakeChapterRequestController=new AbortController();
  try{
    const chapters=await examPage.api.request(
      '/api/chapters?'+new URLSearchParams({subject_id:sid}).toString(),
      {signal:examMistakeChapterRequestController.signal}
    );
    if(
      revision!==examMistakeChapterRequestRevision||
      subjectSelect.value!==sid
    )return;
    chapters.forEach(chapter=>chapterSelect.add(new Option(
      String(chapter.name??''),
      String(safeInteger(chapter.id,''))
    )));
    await Promise.all([loadExamMistakes(),loadExamDailyRecommendations()]);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examMistakeChapterRequestRevision)return;
    resetExamMistakeCandidates('筛选读取失败：'+error.message);
  }
}

function filterExamMistakesByChapter(){
  resetExamMistakeCandidates('正在读取候选错题...');
  void Promise.all([loadExamMistakes(),loadExamDailyRecommendations()]);
}

function examMistakeIdentity(mistake){
  const date=String(mistake.date_added||'').trim()||'日期未记录';
  const qnum=String(mistake.qnum||'').trim();
  if(qnum)return `${date} · 题号 ${qnum}`;
  const preview=cleanProblemContent(mistake.content)
    .replace(/\\([a-zA-Z]+)\s*/g,'$1 ')
    .replace(/\\/g,'')
    .replace(/[{}$]/g,'')
    .replace(/\s+/g,' ')
    .trim();
  return `${date} · ${preview?`题目 ${preview.slice(0,18)}${preview.length>18?'…':''}`:'未填写题号'}`;
}

function updateExamMistakeSelection(){
  const count=document.getElementById('exam-selected-count');
  const summary=document.getElementById('exam-selection-summary');
  if(count)count.textContent=`已选 ${examPage.state.selectedExamMistakeIds.size} 道（最多 ${MAX_EXAM_MISTAKE_REFERENCES} 道）${examMistakeTotal?` · 当前范围 ${examMistakeTotal} 道`:""}`;
  if(!summary)return;

  const selected=examMistakes.filter(m=>examPage.state.selectedExamMistakeIds.has(Number(m.id)));
  if(!selected.length){
    summary.innerHTML=`<span class="exam-selection-summary-icon">${icon('info')}</span><span>勾选错题后，这里会显示所选题目的日期和题号。</span>`;
    summary.classList.remove('has-selection');
    return;
  }

  const visible=selected.slice(0,4)
    .map(m=>`<span class="exam-selected-token">${escapeHTML(examMistakeIdentity(m))}</span>`)
    .join('');
  const remaining=selected.length>4?`<span class="exam-selected-more">另有 ${selected.length-4} 道</span>`:'';
  summary.innerHTML=`<span class="exam-selection-summary-icon">${icon('check')}</span><span class="exam-selection-label">已选错题</span><span class="exam-selected-tokens">${visible}${remaining}</span>`;
  summary.classList.add('has-selection');
}

async function loadExamMistakes(options={}){
  const append=Boolean(options.append);
  const subjectId=document.getElementById('em-subject')?.value||'';
  const chapterId=document.getElementById('em-chapter')?.value||'';
  if(!subjectId){
    resetExamMistakeCandidates();
    return;
  }
  if(append&&(!examMistakeHasMore||!examMistakeNextCursor))return;
  const revision=++examMistakeRequestRevision;
  if(examMistakeRequestController)examMistakeRequestController.abort();
  examMistakeRequestController=new AbortController();
  if(!append){examMistakeNextCursor=null;examMistakeHasMore=false}
  const div=document.getElementById('exam-mistake-list');
  let page,data;
  try{
    const params=new URLSearchParams({limit:'30',subject_id:subjectId});
    if(chapterId)params.set('chapter_id',chapterId);
    if(append&&examMistakeNextCursor)params.set('after',examMistakeNextCursor);
    page=await examPage.api.request('/api/v2/exam-candidates?'+params.toString(),{signal:examMistakeRequestController.signal});
    if(revision!==examMistakeRequestRevision)return;
    data=Array.isArray(page?.items)?page.items:[];
  }catch(error){
    if(error?.name==='AbortError'||revision!==examMistakeRequestRevision)return;
    div.textContent='错题读取失败：'+error.message;
    return;
  }
  if(!Array.isArray(data))data=[];
  mergeExamMistakes(data,{replace:!append});
  examMistakeNextCursor=page?.next_cursor||null;
  examMistakeHasMore=Boolean(page?.has_more&&examMistakeNextCursor);
  examMistakeTotal=Math.max(0,safeInteger(page?.total));
  renderExamMistakes();
}

function renderExamMistakes(){
  const div=document.getElementById('exam-mistake-list');
  if(!div)return;
  const ranks=new Map(examDailyRecommendations.map((item,index)=>[safeInteger(item.id),index]));
  const data=[...examMistakes].sort((a,b)=>(ranks.get(safeInteger(a.id))??ranks.size)-(ranks.get(safeInteger(b.id))??ranks.size)||b.id-a.id);
  updateExamMistakeSelection();
  if(!data.length){
    div.innerHTML=emptyStateHTML('没有可用的未掌握错题',{icon:'notebook',description:'调整筛选条件，或先在错题列表中积累更多题目。',compact:true});
    return;
  }
  div.innerHTML=data.map(m=>{
    const date=String(m.date_added||'').trim()||'日期未记录';
    const qnum=String(m.qnum||'').trim();
    const subject=String(m.subject||'').trim()||'未分类科目';
    const chapter=String(m.chapter||'').trim()||'未分类章节';
    const reasons=String(m.reasons||'').split(',').map(r=>r.trim()).filter(Boolean);
    return `<label class="exam-mistake-option">
      <input class="exam-mistake-checkbox" type="checkbox" value="${safeInteger(m.id)}" onchange="toggleMistake(this)" aria-label="选择 ${escapeHTML(examMistakeIdentity(m))}" ${examPage.state.selectedExamMistakeIds.has(safeInteger(m.id))?'checked':''}>
      <span class="exam-mistake-content">
        <span class="exam-mistake-meta">
          <span class="exam-mistake-date">${icon('calendar')}${escapeHTML(date)}</span>
          <span class="exam-mistake-qnum">${qnum?`题号 ${escapeHTML(qnum)}`:'未填写题号'}</span>
          <span class="exam-mistake-location">${escapeHTML(subject)} · ${escapeHTML(chapter)}</span>
        </span>
        ${ranks.has(safeInteger(m.id))?'<span class="badge">待巩固推荐</span>':''}
        ${problemPreviewHTML(m.content,'exam-mistake-problem')}
        ${m.note?`<span class="exam-mistake-note">${icon('message')}${escapeHTML(m.note)}</span>`:''}
      </span>
      <span class="exam-mistake-reasons">${reasons.map(reason=>`<span class="badge badge-r">${escapeHTML(reason)}</span>`).join('')}</span>
    </label>`;
  }).join('')+(examMistakeHasMore?'<button type="button" class="btn btn-out btn-sm exam-candidates-more" id="exam-candidates-more">加载更多错题</button>':'')+`<div class="list-total">已加载 ${data.length} 道</div>`;
  document.getElementById('exam-candidates-more')?.addEventListener('click',()=>loadExamMistakes({append:true}));
  void renderMath(div);
}

function toggleMistake(cb){
  if(cb.checked){
    if(examPage.state.selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES){
      cb.checked=false;
      showModal('提示',`一次最多选择 ${MAX_EXAM_MISTAKE_REFERENCES} 道参考错题`,'info');
      return;
    }
    examPage.state.selectedExamMistakeIds.add(+cb.value);
  }else{
    examPage.state.selectedExamMistakeIds.delete(+cb.value);
  }
  updateExamMistakeSelection();
}

function selectAllMistakes(){
  document.querySelectorAll('#exam-mistake-list input[type=checkbox]').forEach(c=>{
    if(examPage.state.selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES)return;
    c.checked=true;
    examPage.state.selectedExamMistakeIds.add(+c.value);
  });
  updateExamMistakeSelection();
}

function selectNoMistakes(){
  document.querySelectorAll('#exam-mistake-list input[type=checkbox]').forEach(c=>c.checked=false);
  examPage.state.selectedExamMistakeIds.clear();
  updateExamMistakeSelection();
}

function cancelReads(){
  examMistakeRequestRevision+=1;examMistakeChapterRequestRevision+=1;examDailyRecommendationRevision+=1;
  for(const controller of [examMistakeRequestController,examMistakeChapterRequestController,examDailyRecommendationController])controller?.abort();
}

examPage.selectionMistakes=Object.freeze({resetExamMistakeCandidates,renderExamMistakes,loadExamDailyRecommendations,prepareExamMistakeFilters,loadExamMistakeChapters,filterExamMistakesByChapter,examMistakeIdentity,updateExamMistakeSelection,loadExamMistakes,toggleMistake,selectAllMistakes,selectNoMistakes,cancelReads});
})();
