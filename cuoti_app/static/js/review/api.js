(function initReviewApi(){
  'use strict';

  const review=window.Cuoti.review;

  async function fetchReviewPage(params,signal){
    return api('/api/v2/reviews/due?'+params.toString(),{signal});
  }

  function fetchSubjects(){
    return api('/api/subjects');
  }

  function fetchChapters(subjectId,signal){
    const params=new URLSearchParams({subject_id:String(subjectId)});
    return api('/api/chapters?'+params.toString(),{signal});
  }

  function submitReview(item,result){
    return api('/api/v2/mistakes/'+encodeURIComponent(item.publicId)+'/review',{
      method:'POST',
      body:JSON.stringify({
        result,assistance:window.Cuoti.reviewEvidence?.value(`spaced:${item.publicId}:${item.revision}`)||'none',
        revision:safeInteger(item.revision),
        client_event_id:crypto.randomUUID()
      })
    });
  }

  review.api=Object.freeze({
    fetchReviewPage,fetchSubjects,fetchChapters,submitReview
  });
})();
