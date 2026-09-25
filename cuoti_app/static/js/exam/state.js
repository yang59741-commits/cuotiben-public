(function initExamState(){
  'use strict';
  window.Cuoti.exam=window.Cuoti.exam||{};
  window.Cuoti.exam.state={
    active:false, revision:0,
    selectedExamChapters:new Set(),
    examMode:'free',
    pastExamSources:[],
    currentExamPreviewId:null,
    selectedExamMistakeIds:new Set()
  };
})();
