(function initDailyReviewFacade(){
  'use strict';
  const feature=window.Cuoti.dailyReview;
  if(!feature?.controller)throw new Error('Daily review controller failed to load');
  window.dailyReview=feature.controller;
  feature.page=Object.freeze({
    mount:params=>feature.controller.mount(params),
    unmount:()=>feature.controller.deactivate(),
    refresh:params=>feature.controller.mount(params)
  });
})();
