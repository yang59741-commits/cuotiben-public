(function initMistakeAPI(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.mistakes=window.Cuoti.mistakes||{};

  function list(params,signal){
    return api('/api/v2/mistakes?'+params.toString(),{signal});
  }

  function counts(status){
    const query=status&&status!=='全部'
      ?'?'+new URLSearchParams({status}).toString():'';
    return api('/api/v2/mistake-counts'+query);
  }

  function create(payload){
    return api('/api/v2/mistakes',{method:'POST',body:JSON.stringify(payload)});
  }

  function updateNote(publicId,revision,note){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId),{
      method:'PATCH',body:JSON.stringify({revision,note})
    });
  }

  function toggle(publicId,revision){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/toggle',{
      method:'PUT',body:JSON.stringify({revision})
    });
  }

  function updateReasons(publicId,revision,reasons){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/reasons',{
      method:'PUT',body:JSON.stringify({revision,reasons})
    });
  }

  function chapters(subjectId,signal){
    const query=new URLSearchParams({subject_id:String(subjectId)});
    return api('/api/chapters?'+query.toString(),{signal});
  }

  function updateChapter(publicId,revision,chapterId){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/chapter',{
      method:'PUT',body:JSON.stringify({revision,chapter_id:chapterId})
    });
  }

  function remove(publicId,revision){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId),{
      method:'DELETE',body:JSON.stringify({revision})
    });
  }

  function trash(params=new URLSearchParams(),signal){
    const query=params.toString();
    return api('/api/v2/trash/mistakes'+(query?'?'+query:''),{signal});
  }

  function restore(publicId,revision){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/restore',{
      method:'POST',body:JSON.stringify({revision})
    });
  }

  function purge(publicId,revision){
    return api('/api/v2/trash/mistakes/'+encodeURIComponent(publicId),{
      method:'DELETE',body:JSON.stringify({revision,confirm:true})
    });
  }

  function attachments(publicId){
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/attachments');
  }

  function uploadAttachment(publicId,revision,file){
    const body=new FormData();body.append('revision',String(revision));body.append('file',file);
    return api('/api/v2/mistakes/'+encodeURIComponent(publicId)+'/attachments',{
      method:'POST',body
    });
  }

  function exportHistory(params,signal){
    return api('/api/v2/mistake-exports?'+params.toString(),{signal});
  }

  function bulkPreview(operation){
    return api('/api/v2/mistakes/bulk/preview',{
      method:'POST',body:JSON.stringify(operation)
    });
  }

  function bulkCommit(payload){
    return api('/api/v2/mistakes/bulk/commit',{
      method:'POST',body:JSON.stringify(payload)
    });
  }

  window.Cuoti.mistakes.api=Object.freeze({
    list,counts,create,updateNote,toggle,updateReasons,chapters,updateChapter,remove,
    trash,restore,purge,attachments,uploadAttachment,
    exportHistory,bulkPreview,bulkCommit
  });
})();
