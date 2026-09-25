(function initReviewState(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.review=window.Cuoti.review||{};
  window.Cuoti.review.state={
    items:[],
    pager:createCursorPager(30),
    mountRevision:0,
    requestRevision:0,
    requestController:null,
    pendingIds:new Set(),
    subjectId:'',
    chapterId:'',
    toolbarInitialized:false,
    toolbarPromise:null,
    chapterRequestRevision:0,
    chapterRequestController:null,
    exportInFlight:false
  };
})();
