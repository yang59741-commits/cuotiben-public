(function initDailyAnalysisFacade(){
  'use strict';

  const controller=window.Cuoti?.dailyAnalysis?.controller;
  if(!controller)throw new Error('Daily analysis controller failed to load');
  window.analyzeDay=(date,trigger)=>controller.open(date,'',trigger);
  window.openMistakeDailyAnalysis=(date,publicId,trigger)=>controller.open(date,publicId,trigger);
})();
