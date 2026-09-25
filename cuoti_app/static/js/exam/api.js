(function initExamApi(){
  'use strict';
  const examPage=window.Cuoti.exam;
  const reads=new Set();

  async function request(url,options={}){
    // Submitted writes and background jobs outlive the page. Only reads belong
    // to its lifetime; reject late responses even if transport ignores abort.
    if(options.method&&options.method!=='GET'){
      const revision=examPage.state.revision;
      let result;
      try{
        result=await api(url,options);
      }catch(error){
        if(revision!==examPage.state.revision)throw new DOMException('Page left after write','AbortError');
        throw error;
      }
      // Do not cancel the write; discard only its obsolete page continuation.
      if(revision!==examPage.state.revision)throw new DOMException('Page left after write','AbortError');
      return result;
    }
    const controller=new AbortController();
    const abort=()=>controller.abort();
    if(options.signal?.aborted)abort();
    else options.signal?.addEventListener('abort',abort,{once:true});
    reads.add(controller);
    try{
      if(!examPage.state.active||controller.signal.aborted)throw new DOMException('Page read cancelled','AbortError');
      const result=await api(url,{...options,signal:controller.signal});
      if(controller.signal.aborted)throw new DOMException('Page read cancelled','AbortError');
      return result;
    }catch(error){
      if(controller.signal.aborted)throw new DOMException('Page read cancelled','AbortError');
      throw error;
    }finally{
      reads.delete(controller);
      options.signal?.removeEventListener('abort',abort);
    }
  }

  function cancelReads(){
    for(const controller of reads)controller.abort();
    reads.clear();
  }

  examPage.api=Object.freeze({request,cancelReads,runJobRequest:(kind,payload)=>runJobRequest(kind,payload)});
})();
