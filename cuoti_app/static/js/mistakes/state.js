(function initMistakeState(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.mistakes=window.Cuoti.mistakes||{};

  const state={
    reasonOptions:Object.freeze([
      '计算错误','概念不清','方法不会','卡在技巧','审题失误','时间不够'
    ]),
    subjectFilter:'',
    attentionFilter:'',
    attentionRevision:0,
    analysisDates:{},
    view:'current',
    items:[],
    pager:createCursorPager(30),
    trashPager:createCursorPager(30),
    requestRevision:0,
    requestController:null,
    searchTimer:null,
    selectedPublicIds:new Set(),
    exportInFlight:false,
    exportHistoryItems:[],
    exportPager:createCursorPager(20),
    exportHistoryRevision:0,
    exportHistoryController:null,
    filtersInitialized:false
  };

  window.Cuoti.mistakes.state=state;
})();
