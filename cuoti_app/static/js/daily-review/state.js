(function initDailyReviewState(){
  'use strict';
  window.Cuoti=window.Cuoti||{};
  window.Cuoti.dailyReview=window.Cuoti.dailyReview||{};
  window.Cuoti.dailyReview.state={
    session:null,
    items:[],
    subjectId:'',
    filters:{},
    resultChip:'all',
    sessionId:null,
    grouped:false,
    practiceMode:'mixed',
    currentId:null,
    hintOpen:new Set(),
    expanded:new Set(),
    queued:null,
    drawer:null,
    remediationItems:[],
    generationRequest:null,
    mountRevision:0,
    requestRevision:0,
    requestController:null,
    generationInFlight:false,
    pendingIds:new Set(),
    toolbarInitialized:false,
    toolbarPromise:null,
    lastExport:null,
    // Historical PDF re-exports: in-flight targets by session id, and request
    // keys kept only while a submission's outcome is uncertain.
    reexports:new Map(),
    reexportKeys:new Map()
  };
})();
