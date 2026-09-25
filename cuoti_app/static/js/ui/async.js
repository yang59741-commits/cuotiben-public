(function initAsyncFlow(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.ui=window.Cuoti.ui||{};

  function createLatestRequest(){
    let revision=0;
    let controller=null;
    return Object.freeze({
      begin(){
        controller?.abort();
        controller=new AbortController();
        revision+=1;
        return Object.freeze({revision,signal:controller.signal});
      },
      cancel(){
        controller?.abort();
        controller=null;
        revision+=1;
      },
      current(request){
        return Boolean(request)&&request.revision===revision&&!request.signal.aborted;
      },
      revision:()=>revision
    });
  }

  function errorEnvelope(error){
    const data=error?.data&&typeof error.data==='object'?error.data:{};
    return Object.freeze({
      name:String(error?.name||'Error'),
      message:String(data.message||error?.message||'请求失败'),
      code:String(data.code||error?.code||'request_failed'),
      status:Number(error?.status||0),
      retryable:Boolean(data.retryable??error?.retryable),
      data:Object.freeze({...data}),
      aborted:error?.name==='AbortError'
    });
  }

  async function typesetNew(nodes){
    const unique=[...new Set((nodes||[]).filter(node=>node?.isConnected))];
    await Promise.all(unique.map(node=>renderMath(node)));
    return unique.length;
  }

  window.Cuoti.ui.asyncFlow=Object.freeze({
    createLatestRequest,
    errorEnvelope,
    typesetNew
  });
})();
