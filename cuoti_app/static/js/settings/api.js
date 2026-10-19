(function initSettingsAPI(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.settings=window.Cuoti.settings||{};

  const paths=Object.freeze({
    ai:'/api/v2/settings/ai',
    aiUsage:'/api/v2/settings/ai/usage',
    aiMemory:'/api/v2/settings/ai/memory',
    aiMemoryProfile:'/api/v2/settings/ai/memory/profile',
    aiMemoryPause:'/api/v2/settings/ai/memory/pause',
    aiMemoryRebuild:'/api/v2/settings/ai/memory/rebuild',
    aiTest:'/api/v2/settings/ai/test',
    aiModels:'/api/v2/settings/ai/models',
    aiMigrateLegacy:'/api/v2/settings/ai/migrate-legacy',
    aiReset:'/api/v2/settings/ai/reset',
    aiCredential:'/api/v2/settings/ai/credential',
    health:refresh=>'/api/v2/health'+(refresh?'?refresh=1':''),
    semanticIndex:'/api/v2/settings/semantic-index',
    semanticOverview:'/api/v2/settings/semantic-index/overview',
    semanticCleanupPreview:'/api/v2/settings/semantic-index/cleanup-preview',
    semanticAction:action=>`/api/v2/settings/semantic-index/actions/${action}`,
    questionBankStatus:'/api/v2/question-bank/status',
    questionBankRebuild:'/api/v2/question-bank/rebuild'
  });
  const state=window.Cuoti.settings.state;
  const reads=new Map(),operations=new Map();
  const aborted=()=>new DOMException('Settings page request superseded','AbortError');
  function beginOperation(name){
    const serial=(operations.get(name)||0)+1;
    operations.set(name,serial);
    const revision=state.revision;
    const latest=()=>operations.get(name)===serial;
    return {latest,current:()=>state.active&&state.revision===revision&&latest()};
  }
  function cancelRead(path){reads.get(path.split('?')[0])?.cancel();}
  function cancelReads(){for(const gate of reads.values())gate.cancel();}
  async function request(path,options={}){
    const revision=state.revision;
    if(!state.active)throw aborted();
    const isRead=!options.method||options.method==='GET';
    let gate,read;
    if(isRead){
      const key=path.split('?')[0];
      if(!reads.has(key))reads.set(key,window.Cuoti.ui.asyncFlow.createLatestRequest());
      gate=reads.get(key);read=gate.begin();
    }
    const current=()=>state.active&&state.revision===revision&&(!gate||gate.current(read));
    try{
      const result=await api(path,gate?{...options,signal:read.signal}:options);
      if(!current())throw aborted();
      return result;
    }catch(error){
      if(!current())throw aborted();
      throw error;
    }
  }
  const write=(path,payload,method='POST')=>request(path,{
    method,
    headers:{'X-Cuoti-CSRF':'same-origin'},
    body:JSON.stringify(payload)
  });
  const aiMemoryProfilePage=(query,options={})=>{
    const params=new URLSearchParams({
      memory_type:String(query.memoryType||'free'),
      page:String(query.page||1),
      page_size:String(query.pageSize||20)
    });
    if(query.status)params.set('status',String(query.status));
    if(query.entryType)params.set('entry_type',String(query.entryType));
    return request(`${paths.aiMemoryProfile}?${params}`,{...options,method:'GET'});
  };
  const aiMemoryProfileCommand=payload=>write(paths.aiMemoryProfile,payload,'PATCH');

  window.Cuoti.settings.api=Object.freeze({paths,request,write,aiMemoryProfilePage,aiMemoryProfileCommand,beginOperation,cancelRead,cancelReads});
})();
