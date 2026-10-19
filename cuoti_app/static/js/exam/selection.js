(function initExamSelection(){
'use strict';
const examPage=window.Cuoti.exam;
function switchExamMode(mode){
  examPage.state.examMode=mode;
  ['free','mistakes','past'].forEach(name=>{
    const active=name===mode;
    const panel=document.getElementById('exam-'+name);
    const tab=document.getElementById('tab-'+name);
    panel.hidden=!active;
    tab.classList.toggle('on',active);
    tab.setAttribute('aria-selected',String(active));
    tab.tabIndex=active?0:-1;
  });
  if(mode==='free'){examPage.selectionFree.loadExamSubjects()}
  if(mode==='mistakes'){void examPage.selectionMistakes.prepareExamMistakeFilters()}
  if(mode==='past'){examPage.selectionPast.loadPastExamSources()}
  examPage.controller.setGenerateButton();
}

function updateDifficultyStars(select){
  const target=document.getElementById(select.id+'-stars');
  if(!target)return;
  const levels={基础:1,中等:2,困难:3};
  const level=levels[select.value]||2;
  target.textContent='★'.repeat(level)+'☆'.repeat(3-level);
  target.setAttribute('aria-label',`难度 ${level} 星，共 3 星`);
}

let binding=null;
function bind(){
  if(binding)return;
  binding=new AbortController();
['e-diff','em-diff'].forEach(id=>{
  const select=document.getElementById(id);
  if(!select)return;
  select.addEventListener('change',()=>updateDifficultyStars(select),{signal:binding.signal});
  updateDifficultyStars(select);
});

document.getElementById('em-subject')?.addEventListener(
  'change',
  ()=>void examPage.selectionMistakes.loadExamMistakeChapters(),
  {signal:binding.signal});

document.getElementById('em-chapter')?.addEventListener(
  'change',
  examPage.selectionMistakes.filterExamMistakesByChapter,
  {signal:binding.signal});

document.getElementById('exam-daily-recommendations-refresh')?.addEventListener(
  'click',
  ()=>void examPage.selectionMistakes.loadExamDailyRecommendations(),
  {signal:binding.signal});

document.querySelector('.tabs')?.addEventListener('keydown',event=>{
  const tabs=[...document.querySelectorAll('[role="tab"][data-exam-mode]')];
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
  switchExamMode(tabs[next].dataset.examMode);
},{signal:binding.signal});
}
function unbind(){
  binding?.abort();binding=null;
  examPage.selectionFree.cancelReads();examPage.selectionMistakes.cancelReads();
}

examPage.selection=Object.freeze({
  loadExamSubjects:examPage.selectionFree.loadExamSubjects,
  isExcludedExamChapter:examPage.selectionFree.isExcludedExamChapter,
  resetExamBankCoverage:examPage.selectionFree.resetExamBankCoverage,
  renderExamBankCoverage:examPage.selectionFree.renderExamBankCoverage,
  loadExamBankCoverage:examPage.selectionFree.loadExamBankCoverage,
  loadExamChapters:examPage.selectionFree.loadExamChapters,
  pickChapter:examPage.selectionFree.pickChapter,
  switchExamMode,
  loadPastExamSources:examPage.selectionPast.loadPastExamSources,
  loadPastExamSubjects:examPage.selectionPast.loadPastExamSubjects,
  pastExamAvailabilityMessage:examPage.selectionPast.pastExamAvailabilityMessage,
  pastExamSourceUsable:examPage.selectionPast.pastExamSourceUsable,
  pastExamYearStructures:examPage.selectionPast.pastExamYearStructures,
  updatePastCandidateNote:examPage.selectionPast.updatePastCandidateNote,
  renderPastExamSource:examPage.selectionPast.renderPastExamSource,
  resetExamMistakeCandidates:examPage.selectionMistakes.resetExamMistakeCandidates,
  renderExamMistakes:examPage.selectionMistakes.renderExamMistakes,
  loadExamDailyRecommendations:examPage.selectionMistakes.loadExamDailyRecommendations,
  prepareExamMistakeFilters:examPage.selectionMistakes.prepareExamMistakeFilters,
  loadExamMistakeChapters:examPage.selectionMistakes.loadExamMistakeChapters,
  filterExamMistakesByChapter:examPage.selectionMistakes.filterExamMistakesByChapter,
  examMistakeIdentity:examPage.selectionMistakes.examMistakeIdentity,
  updateExamMistakeSelection:examPage.selectionMistakes.updateExamMistakeSelection,
  loadExamMistakes:examPage.selectionMistakes.loadExamMistakes,
  toggleMistake:examPage.selectionMistakes.toggleMistake,
  selectAllMistakes:examPage.selectionMistakes.selectAllMistakes,
  selectNoMistakes:examPage.selectionMistakes.selectNoMistakes,
  updateDifficultyStars,
  bind,
  unbind
});
})();
