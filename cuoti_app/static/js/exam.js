(function initExamPage(){
  'use strict';
  const examPage=window.Cuoti.exam;
  Object.assign(window,{
    toggleMistake:examPage.selection.toggleMistake,
    loadExamChapters:examPage.selection.loadExamChapters,
    loadPastExamSubjects:examPage.selection.loadPastExamSubjects,
    renderPastExamSource:examPage.selection.renderPastExamSource
  });
  window.Cuoti.features.register('exam',Object.freeze({
    mount:examPage.controller.mount, unmount:examPage.controller.unmount,
    refresh:examPage.history.loadExamHistory,
    switchMode:examPage.selection.switchExamMode,
    selectAll:examPage.selection.selectAllMistakes,
    selectNone:examPage.selection.selectNoMistakes,
    generate:examPage.controller.generateExam
  }));
})();
