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
  const request=(path,options)=>api(path,options);
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

  window.Cuoti.settings.api=Object.freeze({paths,request,write,aiMemoryProfilePage,aiMemoryProfileCommand});
})();
