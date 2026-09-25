(function initDailyReviewApi(){
  'use strict';
  const feature=window.Cuoti.dailyReview;
  function current(subjectId,signal,filters={},sessionId=null){
    const params=new URLSearchParams(Object.entries(filters).filter(([,v])=>v!==''&&v!=null));
    if(subjectId)params.set('subject_id',String(subjectId));
    const query=params.toString();
    return api('/api/v2/daily-review/'+(sessionId?'sessions/'+encodeURIComponent(sessionId):'current')+(query?'?'+query:''),{signal});
  }
  function subjects(){return api('/api/subjects');}
  function submit(roundId,itemId,result,key,assistance='unknown'){
    return api(`/api/v2/daily-review/sessions/${encodeURIComponent(roundId)}/items/${encodeURIComponent(itemId)}/result`,{
      method:'POST',
      // Result bodies are small; keep both queued and in-flight saves alive
      // when pagehide flushes the undo window during refresh or tab close.
      keepalive:true,
      headers:key?{'Idempotency-Key':String(key)}:undefined,
      body:JSON.stringify({result,assistance,idempotency_key:key||undefined})
    });
  }
  feature.api=Object.freeze({current,subjects,submit});
})();
