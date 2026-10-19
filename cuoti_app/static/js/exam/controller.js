(function initExamController(){
'use strict';
const examPage=window.Cuoti.exam;

function setGenerateButton(loading=false){
  const btn=document.getElementById('btn-gen');
  const label=examPage.state.examMode==='past'?'分析真题并智能选题':'生成试卷';
  btn.innerHTML=loading?icon('loader','icon-spin')+(examPage.state.examMode==='past'?'分析并选题中...':'生成中...'):icon('sparkles')+label;
}

function resetGenerateUI(){
  const button=document.getElementById('btn-gen');
  if(button)button.disabled=false;
  const progress=document.getElementById('gen-progress');
  if(progress)progress.style.display='none';
  setGenerateButton();
}

async function generateExam(){
  const prefix=examPage.state.examMode==='mistakes'?'em-':'e-';
  const types=examPage.state.examMode==='past'?[]:[...document.querySelectorAll('#'+prefix+'types input:checked')].map(c=>c.value);
  if(examPage.state.examMode!=='past'&&!types.length){showFieldError(document.getElementById(prefix+'types'),'请至少选择一种题型');return}
  const btn=document.getElementById('btn-gen');const st=document.getElementById('gen-status');
  btn.disabled=true;setGenerateButton(true);
  st.textContent=examPage.state.examMode==='past'
    ?'AI 正在学习已选年份的真题结构，并从指定候选题源筛选题目...'
    :'AI 正在出题...';
  document.getElementById('gen-progress').style.display='block';
  try{
    // Validation stops before any request, so drop the optimistic progress text.
    const cancelBeforeRequest=()=>{st.textContent='';resetGenerateUI()};
    let payload;
    if(examPage.state.examMode==='mistakes'){
      if(!examPage.state.selectedExamMistakeIds.size){showFieldError(document.getElementById('exam-mistake-list'),'请勾选至少一道错题');cancelBeforeRequest();return}
      payload={
        source:'mistakes',mistake_ids:[...examPage.state.selectedExamMistakeIds],
        difficulty:document.getElementById('em-diff').value,
        target:'日常练习',
        training_target:document.getElementById('em-training-target').value,
        question_count:parseInt(document.getElementById('em-count').value)||examPage.state.selectedExamMistakeIds.size,
        types,note:document.getElementById('em-note').value.trim()
      };
    }else if(examPage.state.examMode==='past'){
      const sid=document.getElementById('ep-subject').value;
      const institution=document.getElementById('ep-institution').value;
      const referenceYear=safeInteger(document.getElementById('ep-reference-year').value);
      const candidateSource=document.getElementById('ep-candidate-source').value;
      const allowedCandidateSources=new Set(['ordinary','other_past','mixed']);
      const source=examPage.state.pastExamSources.find(item=>
        String(item.institution)===institution&&
        Number(item.subject_id)===Number(sid)
      );
      const structure=examPage.selection.pastExamYearStructures(source)
        .find(item=>item.year===referenceYear);
      if(!institution||!sid||!referenceYear||!structure){
        showModal('提示', '请选择有可用真题 TeX 的院校、科目和结构年份', 'info');
        cancelBeforeRequest();return;
      }
      if(!examPage.selection.pastExamSourceUsable(source)||!structure.usable){
        showModal(
          '当前组合不可提交',
          structure.availability_reason||examPage.selection.pastExamAvailabilityMessage(source),
          'info'
        );
        cancelBeforeRequest();return;
      }
      if(!allowedCandidateSources.has(candidateSource)){
        showModal('提示', '候选题源选项无效，请重新选择', 'info');
        cancelBeforeRequest();return;
      }
      if(candidateSource==='mixed'&&safeInteger(structure.question_count)<2){
        showModal('提示', '混合题源至少需要 2 题，请改选结构年份或候选题源', 'info');
        cancelBeforeRequest();return;
      }
      const modeInfo=structure.source_modes?.[candidateSource];
      if(modeInfo && modeInfo.available===false){
        showModal('当前组合不可提交',String(modeInfo.reason||'该题源方式当前不可用'),'info');
        cancelBeforeRequest();return;
      }
      payload={
        source:'past',
        subject_id:+sid,
        institution,
        reference_year:referenceYear,
        candidate_source:candidateSource,
        paper_count:parseInt(document.getElementById('ep-count').value)||1,
        allow_adaptation:document.getElementById('ep-adaptation').checked,
        note:document.getElementById('ep-note').value.trim()
      };
      if(document.getElementById('ep-priority-once').checked){
        payload.priority_once=true;
      }
    }else{
      const sid=document.getElementById('e-subject').value;
      if(!sid){showFieldError(document.getElementById('e-subject'),'请先选择科目');cancelBeforeRequest();return}
      if(!examPage.state.selectedExamChapters.size){showFieldError(document.getElementById('e-chapters'),'请至少选择一个章节');cancelBeforeRequest();return}
      payload={
        source:'free',subject_id:+sid,chapters:[...examPage.state.selectedExamChapters],
        difficulty:document.getElementById('e-diff').value,
        target:document.getElementById('e-target').value,
        question_count:parseInt(document.getElementById('e-count').value)||5,
        types,note:document.getElementById('e-note').value.trim()
      };
    }
    const r=await examPage.api.runJobRequest('exam-generation',payload);
    if(r.error){st.textContent=r.error;resetGenerateUI();return}
    resetGenerateUI();
    const generatedCount=Math.max(1,safeInteger(r.count,1));
    examPage.preview.renderMaterialUnderstanding(r.material_understanding);
    st.innerHTML=icon('check','inline-icon')+`生成完成${generatedCount>1?`（${generatedCount}份）`:''}`;
    // Show download modal
    if(r.pdf_url){
      const generatedExams=Array.isArray(r.exams)&&r.exams.length?r.exams:[r];
      const downloadButtons=generatedExams.map((exam,index)=>{
        const pdfURL=safeLocalURL(exam.pdf_url);
        if(!pdfURL)return '';
        return '<a class="btn btn-s btn-sm" href="'+escapeHTML(pdfURL)+'" target="_blank" rel="noopener" onclick="closeCurrentModalAfter(300)">'+
          icon('download')+(generatedExams.length>1?`下载第 ${index+1} 份`:'下载PDF')+'</a>';
      }).join('');
      showModal('试卷生成成功', generatedExams.length>1?`已生成 ${generatedExams.length} 份试卷，可分别下载。`:'试卷已编译为 PDF，点击下方按钮即可下载。', 'success',
        '<button class="btn btn-out btn-sm" onclick="hideModal()">关闭</button>' +
        downloadButtons);
    }
    examPage.history.loadExamHistory();
  }catch(e){st.textContent='请求失败：'+e.message}
  resetGenerateUI();
}

async function recompileExam(id){
  const button=document.getElementById('exam-recompile');
  if(button)button.disabled=true;
  try{
    const result=await examPage.api.runJobRequest('exam-recompile',{exam_id:id});
    showModal('重新编译完成',`产物版本 ${safeInteger(result?.artifact_revision)} 已发布。`,'success');
    await examPage.history.loadExamHistory();
  }catch(error){
    if(error?.name==='AbortError')return;
    showModal('重新编译失败',error.message,'error');
  }finally{
    if(button)button.disabled=false;
  }
}

function mount(){
  examPage.state.active=true;
  examPage.selection.bind();examPage.history.bind();examPage.preview.bind();examPage.issues.bind();
  if(examPage.state.examMode==='past')void examPage.selection.loadPastExamSources();
  else if(examPage.state.examMode==='mistakes'){
    if(!examPage.state.selectedExamMistakeIds.size)void examPage.selection.prepareExamMistakeFilters();
  }
  else void examPage.selection.loadExamSubjects();
  return examPage.history.loadExamHistory();
}

function unmount(){
  examPage.state.active=false;
  examPage.state.revision+=1;
  examPage.api.cancelReads();
  examPage.selection.unbind();examPage.history.unbind();examPage.preview.unbind();examPage.issues.unbind();
  examPage.preview.hideExamPreview();
}

examPage.controller=Object.freeze({
  mount,
  unmount,
  setGenerateButton,
  resetGenerateUI,
  generateExam,
  recompileExam
});
})();
