(function initDailyAnalysisApi(){
  'use strict';

  const feature=window.Cuoti.dailyAnalysis;

  function fetchCache(date,mistakePublicId=''){
    const params=new URLSearchParams();
    if(mistakePublicId)params.set('mistake_public_id',mistakePublicId);
    const query=params.toString();
    return api('/api/v2/daily-analyses/'+encodeURIComponent(date)+(query?'?'+query:''));
  }

  function generate(date,generationScope,mistakePublicId=''){
    const payload={
      operation:'day',date,generation_scope:generationScope,
      generation_request_id:crypto.randomUUID()
    };
    if(mistakePublicId)payload.mistake_public_id=mistakePublicId;
    return runJobRequest('analysis',payload);
  }

  feature.api=Object.freeze({fetchCache,generate});
})();
