let aiSettingsLoaded=false;
let aiSettingsVersion=null;
let runtimeHealthLoaded=false;
let aiSettingsBaseline=null;
let aiSettingsPayload=null;
let aiModelCatalog=null;
const settingsState=window.Cuoti.settings.state;
const settingsAPI=window.Cuoti.settings.api;
const settingsRender=window.Cuoti.settings.render;
const aiSettingsDirty=settingsState.dirtyFields;
let pendingSettingsConflict=null;
let semanticOperations=null;
let settingsRouteActive=false;
let semanticStatusTimer=null;
let semanticStatusRequest=null;
let semanticConfirmations={provider:false,auto_refresh:false,allow_partial:false};

document.querySelectorAll('[data-settings-help]').forEach(button=>{
  button.addEventListener('click',()=>{
    const region=document.getElementById(button.getAttribute('aria-controls'));
    if(!region)return;
    const expanded=button.getAttribute('aria-expanded')==='true';
    button.setAttribute('aria-expanded',String(!expanded));
    region.hidden=expanded;
    const label=button.querySelector('span:last-child');
    if(label)label.textContent=expanded?'了解此功能':'收起说明';
  });
});

const AI_SETTING_FIELDS=settingsRender.fields;
const normalizeAISettings=settingsRender.normalize;

function setAISettingsStatus(message,tone=''){
  const status=document.getElementById('ai-settings-status');
  if(!status)return;
  status.textContent=message;
  status.className=tone?`settings-message ${tone}`:'settings-message';
}

const AI_MODEL_TIER_LABELS=Object.freeze({fast:'快速',balanced:'均衡',quality:'高质量'});

function setAIModelsStatus(message,tone=''){
  const status=document.getElementById('ai-models-status');
  if(!status)return;
  status.textContent=message;
  status.className=tone?`settings-message ${tone}`:'settings-message';
}

function syncReasoningEffortButtons(){
  const value=document.getElementById('ai-reasoning-effort')?.value||'';
  document.querySelectorAll('[data-ai-effort]').forEach(button=>{
    const selected=button.dataset.aiEffort===value;
    button.setAttribute('aria-checked',String(selected));
    button.tabIndex=selected?0:-1;
  });
}

function clearAIModelCatalog(message='尚未读取'){
  aiModelCatalog=null;
  document.getElementById('ai-models-list')?.replaceChildren();
  document.querySelectorAll('[data-ai-speed]').forEach(button=>{
    button.disabled=true;
    button.classList.remove('is-active');
  });
  setAIModelsStatus(message);
}

function refreshAIModelSelections(){
  const selected={
    fast:document.getElementById('ai-fast-model')?.value||'',
    quality:document.getElementById('ai-quality-model')?.value||''
  };
  document.querySelectorAll('[data-ai-model-role]').forEach(button=>{
    const active=selected[button.dataset.aiModelRole]===button.dataset.aiModelId;
    button.classList.toggle('is-selected',active);
    button.setAttribute('aria-pressed',String(active));
  });
}

function chooseAIModel(modelId,role){
  if(!['fast','quality'].includes(role)||!modelId)return;
  const input=document.getElementById(role==='fast'?'ai-fast-model':'ai-quality-model');
  if(!input)return;
  input.value=modelId;
  aiSettingsDirty.add(role==='fast'?'fast_model':'quality_model');
  document.querySelectorAll('[data-ai-speed]').forEach(button=>button.classList.remove('is-active'));
  refreshAIModelSelections();
  setAISettingsStatus('模型选择已更新；保存后生效。');
}

function renderAIModels(payload){
  const root=document.getElementById('ai-models-list');
  if(!root)return;
  root.replaceChildren();
  const models=Array.isArray(payload?.models)?payload.models:[];
  models.forEach(model=>{
    const id=String(model?.id||'').trim();
    if(!id)return;
    const tier=Object.hasOwn(AI_MODEL_TIER_LABELS,model.speed_tier)?model.speed_tier:'balanced';
    const row=document.createElement('div');
    row.className='ai-model-item';
    const name=document.createElement('div');
    name.className='ai-model-name';
    const title=document.createElement('strong');
    title.textContent=String(model.display_name||id);
    const detail=document.createElement('span');
    detail.textContent=id;
    name.append(title,detail);
    const badge=document.createElement('span');
    badge.className=`ai-model-tier ai-model-tier-${tier}`;
    badge.textContent=AI_MODEL_TIER_LABELS[tier];
    const actions=document.createElement('div');
    actions.className='ai-model-actions';
    [['fast','设为快速'],['quality','设为高质量']].forEach(([role,label])=>{
      const button=document.createElement('button');
      button.type='button';
      button.className='btn btn-out btn-xs';
      button.dataset.aiModelRole=role;
      button.dataset.aiModelId=id;
      button.textContent=label;
      button.addEventListener('click',()=>chooseAIModel(id,role));
      actions.appendChild(button);
    });
    row.append(name,badge,actions);
    root.appendChild(row);
  });
  aiModelCatalog=payload;
  document.querySelectorAll('[data-ai-speed]').forEach(button=>{button.disabled=!models.length});
  refreshAIModelSelections();
  setAIModelsStatus(`已读取 ${models.length} 个账号可用文本模型。`,'success');
}

async function loadAvailableAIModels(){
  const button=document.getElementById('ai-models-load');
  const currentProfile=document.getElementById('ai-profile')?.value||'';
  const savedProfile=normalizeAISettings(aiSettingsPayload||{}).id;
  if(currentProfile!==savedProfile||document.getElementById('ai-api-key')?.value.trim()){
    setAIModelsStatus('请先保存当前连接与密钥，再读取模型。','error');
    return;
  }
  button.disabled=true;
  setAIModelsStatus('正在读取账号可用模型...');
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.aiModels,{method:'POST'});
    if(String(payload?.profile_id||'')!==currentProfile)throw new Error('连接已切换，请重新读取');
    renderAIModels(payload);
  }catch(error){
    clearAIModelCatalog('');
    setAIModelsStatus(`模型读取失败：${error.message}`,'error');
  }finally{button.disabled=false}
}

function applyAISpeedPreference(mode){
  const suggestions=aiModelCatalog?.recommendations||{};
  const selections={
    fast:[suggestions.fast,suggestions.fast],
    balanced:[suggestions.fast,suggestions.balanced],
    quality:[suggestions.balanced,suggestions.quality]
  }[mode];
  if(!selections?.every(Boolean))return;
  chooseAIModel(selections[0],'fast');
  chooseAIModel(selections[1],'quality');
  document.querySelectorAll('[data-ai-speed]').forEach(button=>button.classList.toggle('is-active',button.dataset.aiSpeed===mode));
}

const settingsSnapshot=settingsRender.snapshot;

function currentSettingsSnapshot(){
  const values={};
  Object.entries(AI_SETTING_FIELDS).forEach(([field,id])=>{values[field]=document.getElementById(id)?.value??''});
  return values;
}

function syncDNSSettingsState(){
  const mode=document.getElementById('ai-dns-mode')?.value||'domestic';
  const servers=document.getElementById('ai-dns-servers');
  if(!servers)return;
  const usesDomesticDNS=mode==='domestic';
  servers.disabled=!usesDomesticDNS;
  servers.setAttribute('aria-disabled',String(!usesDomesticDNS));
  servers.title=usesDomesticDNS?'可填写 1 至 3 个 IPv4 DNS 地址':'当前网络模式不使用此项';
}

function applyAISettings(payload,{preserveSecret=false,resetDirty=true}={}){
  const settings=normalizeAISettings(payload);
  const secret=preserveSecret?document.getElementById('ai-api-key').value:'';
  aiSettingsPayload=payload;
  aiSettingsVersion=settings.version;
  document.getElementById('ai-profile').value=settings.id;
  document.getElementById('ai-provider').value=settings.adapter;
  document.getElementById('ai-base-url').value=settings.baseURL;
  document.getElementById('ai-dns-mode').value=settings.dnsMode;
  document.getElementById('ai-dns-servers').value=settings.dnsServers;
  document.getElementById('ai-quality-model').value=settings.qualityModel;
  document.getElementById('ai-fast-model').value=settings.fastModel;
  document.getElementById('ai-reasoning-effort').value=settings.reasoningEffort;
  syncReasoningEffortButtons();
  document.getElementById('ai-api-key').value=secret;
  const environmentManaged=['env','environment'].includes(settings.source.toLowerCase());
  document.getElementById('ai-key-state').textContent=settings.keyConfigured
    ?`已配置密钥（来源：${environmentManaged?'环境变量':'本机设置'}）`
    :'尚未配置密钥';
  document.getElementById('settings-source').textContent=environmentManaged?'环境变量优先':'本机设置';
  const assignmentValue=(name,fallback)=>settings.assignments?.[name]?.model_role||settings.assignments?.[name]||fallback;
  document.getElementById('ai-route-classification').value=assignmentValue('classification','fast');
  document.getElementById('ai-route-analysis').value=assignmentValue('problem_analysis','quality');
  document.getElementById('ai-route-daily').value=assignmentValue('daily_analysis','quality');
  document.getElementById('ai-route-exam').value=assignmentValue('exam_generation','quality');
  syncDNSSettingsState();
  const fieldMap={base_url:'ai-base-url',api_key:'ai-api-key',quality_model:'ai-quality-model',fast_model:'ai-fast-model'};
  const managed=settings.lockedFields.map(field=>String(field).toLowerCase());
  Object.entries(fieldMap).forEach(([field,id])=>{
    const input=document.getElementById(id);
    const locked=managed.includes(field)||managed.some(name=>name.endsWith(field))||(environmentManaged&&field==='api_key');
    input.disabled=locked;
    input.setAttribute('aria-disabled',String(locked));
    if(locked)input.title='此项由环境变量管理';
  });
  if(resetDirty){
    aiSettingsBaseline=settingsSnapshot(settings);
    aiSettingsDirty.clear();
  }
  window.Cuoti.ui.selects?.syncAll(document.getElementById('p-settings'));
}

async function loadAISettings(force=false){
  if(aiSettingsLoaded&&!force)return;
  setAISettingsStatus('正在读取设置...');
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.ai);
    applyAISettings(payload);
    document.getElementById('ai-migrate').hidden=!Boolean(payload?.legacy_detected||payload?.can_migrate_legacy||payload?.credential_source==='legacy_file');
    aiSettingsLoaded=true;
    setAISettingsStatus('');
    void loadAIUsage();
    void loadAIMemory();
    void loadAIMemoryProfile(1);
  }catch(error){
    const message=error.status===404?'当前版本尚未提供设置接口。':`设置读取失败：${error.message}`;
    setAISettingsStatus(message,'error');
  }
}

async function loadAIUsage(){
  const root=document.getElementById('ai-usage-stats');if(!root)return;
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.aiUsage);
    const tasks=Array.isArray(payload?.tasks)?payload.tasks:[];
    const business=Array.isArray(payload?.business_tasks)?payload.business_tasks:[];
    const providerHTML=tasks.map(item=>`<div class="health-check"><strong>调用 · ${escapeHTML(item.task)}</strong><span>${safeInteger(item.count)} 次 · Token P50 ${safeInteger(item.p50_tokens)} / P95 ${safeInteger(item.p95_tokens)} · 截断 ${safeInteger(item.truncations)} · 重试 ${safeInteger(item.retries)} · 调用成功率 ${item.success_rate==null?'--':Math.round(Number(item.success_rate)*100)+'%'}</span></div>`).join('');
    const businessHTML=business.map(item=>`<div class="health-check"><strong>任务 · ${escapeHTML(item.task)}</strong><span>${safeInteger(item.count)} 个终态 · 尝试 ${safeInteger(item.attempts)} · 失败 ${safeInteger(item.failed)} · 取消 ${safeInteger(item.cancelled)} · 任务成功率 ${item.success_rate==null?'--':Math.round(Number(item.success_rate)*100)+'%'}</span></div>`).join('');
    root.innerHTML=providerHTML+businessHTML||'<div class="settings-message">尚无使用记录</div>';
  }catch(error){root.textContent='使用统计读取失败：'+error.message}
}
document.getElementById('ai-usage-refresh')?.addEventListener('click',loadAIUsage);

const AI_MEMORY_LABELS={free:'自由组卷',mistakes:'错题学习',past:'真题组卷'};
async function loadAIMemory(){
  const root=document.getElementById('ai-memory-list');if(!root)return;
  const status=document.getElementById('ai-memory-status');
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.aiMemory);
    const rows=Array.isArray(payload?.namespaces)?payload.namespaces:[];
    root.innerHTML=rows.map(item=>`<div class="health-check"><strong>${escapeHTML(AI_MEMORY_LABELS[item.memory_type]||item.memory_type)}</strong><span>${safeInteger(item.event_count)} 条事件 · ${safeInteger(item.profile_count)} 条画像 · ${safeInteger(item.confirmed_count)} 条已确认</span><div class="settings-actions"><button type="button" class="btn btn-out btn-xs" onclick="toggleAIMemory('${item.memory_type}',${item.paused?'false':'true'})">${item.paused?'继续学习':'暂停学习'}</button><button type="button" class="btn btn-out btn-xs" onclick="rebuildAIMemory('${item.memory_type}')">重建摘要</button><button type="button" class="btn btn-d btn-xs" onclick="confirmClearAIMemory('${item.memory_type}')">清空</button></div></div>`).join('')||'<div class="settings-message">尚无记忆</div>';
    if(status){
      const pending=safeInteger(payload.pending_cleanup_count),failed=safeInteger(payload.failed_retry_count);
      status.textContent=`记忆结构版本 ${safeInteger(payload.schema_version)} · 待清理 ${pending} · 重试失败 ${failed}`;
    }
  }catch(error){if(status)status.textContent='记忆状态读取失败：'+error.message}
}
async function toggleAIMemory(memoryType,paused){
  await settingsAPI.request(settingsAPI.paths.aiMemoryPause,{method:'PUT',body:JSON.stringify({memory_type:memoryType,paused})});
  await loadAIMemory();
}
async function rebuildAIMemory(memoryType){
  const status=document.getElementById('ai-memory-status');if(status)status.textContent='正在重建摘要...';
  try{await settingsAPI.request(settingsAPI.paths.aiMemoryRebuild,{method:'POST',body:JSON.stringify({memory_type:memoryType})});await loadAIMemory()}
  catch(error){if(status)status.textContent='重建失败：'+error.message}
}
function confirmClearAIMemory(memoryType){
  showModal('清空 AI 记忆',`将永久清空“${AI_MEMORY_LABELS[memoryType]||memoryType}”记忆，其他类型不受影响。`,'error',
    `<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-d btn-sm" onclick="clearAIMemory('${memoryType}')">确认清空</button>`);
}
async function clearAIMemory(memoryType){
  hideModal();const status=document.getElementById('ai-memory-status');if(status)status.textContent='正在清空...';
  try{await settingsAPI.request(settingsAPI.paths.aiMemory,{method:'DELETE',body:JSON.stringify({memory_type:memoryType,confirm:true})});await loadAIMemory()}
  catch(error){if(status)status.textContent='清空失败：'+error.message}
}
document.getElementById('ai-memory-refresh')?.addEventListener('click',loadAIMemory);
window.toggleAIMemory=toggleAIMemory;window.rebuildAIMemory=rebuildAIMemory;window.confirmClearAIMemory=confirmClearAIMemory;window.clearAIMemory=clearAIMemory;

  const AI_PROFILE_COMMANDS=new Set(['confirm','correct','reject','suppress']);
  const AI_PROFILE_COMMAND_LABELS={confirm:'确认',correct:'纠正',reject:'否定',suppress:'隐藏'};
  const profileGate=window.Cuoti.ui.asyncFlow.createLatestRequest();
  const aiProfileState={page:1,pageSize:20,total:0,hasNext:false,requestSerial:0,commandPending:false,commandTarget:null};
  function setAIMemoryProfileMessage(message,tone=''){
    const output=document.getElementById('ai-memory-profile-message');if(!output)return;
    output.textContent=message;
    output.className=tone?`settings-message ${tone}`:'settings-message';
  }
  function setAIMemoryProfileControls({loading=false}={}){
    const root=document.getElementById('ai-memory-profile-list');
    const previous=document.getElementById('ai-memory-profile-prev');
    const next=document.getElementById('ai-memory-profile-next');
    if(root)root.setAttribute('aria-busy',String(loading||aiProfileState.commandPending));
    if(previous)previous.disabled=loading||aiProfileState.commandPending||aiProfileState.page<=1;
    if(next)next.disabled=loading||aiProfileState.commandPending||!aiProfileState.hasNext;
    for(const id of ['ai-memory-profile-type','ai-memory-profile-entry-type','ai-memory-profile-status','ai-memory-profile-refresh']){
      const control=document.getElementById(id);if(!control)continue;
      control.disabled=aiProfileState.commandPending||(loading&&id==='ai-memory-profile-refresh');
      control.setAttribute('aria-disabled',String(control.disabled));
    }
    document.querySelectorAll('#ai-memory-profile-list [data-ai-profile-action]').forEach(button=>{
      button.disabled=aiProfileState.commandPending||button.dataset.profileCurrent==='true';
    });
  }
  function aiProfileSafeError(error,operation){
    if(error?.status===404)return '这条画像已不存在，请刷新列表。';
    if(error?.status===400)return operation==='读取'?'画像筛选条件无效。':'画像操作无效，请刷新后重试。';
    if(error?.status===503)return '画像服务暂时不可用，请稍后重试。';
    return `${operation}画像失败，请稍后重试。`;
  }
  async function loadAIMemoryProfile(page=1,{announce=true}={}){
    const root=document.getElementById('ai-memory-profile-list');if(!root)return false;
    const memoryType=document.getElementById('ai-memory-profile-type')?.value||'free';
    const status=document.getElementById('ai-memory-profile-status')?.value||'';
    const entryType=document.getElementById('ai-memory-profile-entry-type')?.value||'';
    const requestedPage=Math.max(1,safeInteger(page,1));
    const serial=++aiProfileState.requestSerial;
    const request=profileGate.begin();
    const current=()=>serial===aiProfileState.requestSerial&&profileGate.current(request);
    root.replaceChildren();
    const loading=document.createElement('div');loading.className='settings-message';loading.textContent='正在读取画像...';root.appendChild(loading);
    setAIMemoryProfileControls({loading:true});
    if(announce)setAIMemoryProfileMessage('正在读取画像...');
    try{
      const payload=await settingsAPI.aiMemoryProfilePage({
        memoryType,page:requestedPage,pageSize:aiProfileState.pageSize,status,entryType
      },{signal:request.signal});
      if(!current())return false;
      if(!payload||typeof payload!=='object'||!Array.isArray(payload.items))throw new Error('INVALID_PROFILE_PAGE');
      const responsePage=Math.max(1,safeInteger(payload.page,requestedPage));
      const total=Math.max(0,safeInteger(payload.total));
      const pageSize=Math.max(1,Math.min(100,safeInteger(payload.page_size,aiProfileState.pageSize)));
      const lastPage=Math.max(1,Math.ceil(total/pageSize));
      if(responsePage>lastPage)return loadAIMemoryProfile(lastPage,{announce});
      aiProfileState.page=responsePage;
      aiProfileState.pageSize=pageSize;
      aiProfileState.total=total;
      aiProfileState.hasNext=Boolean(payload.has_next)&&responsePage<lastPage;
      const rendered=settingsRender.aiProfileList(payload.items.slice(0,pageSize));
      document.getElementById('ai-memory-profile-page').textContent=`第 ${responsePage} / ${lastPage} 页 · 共 ${total} 条`;
      if(announce)setAIMemoryProfileMessage(`已读取 ${rendered} 条画像。`,'success');
      return true;
    }catch(error){
      if(error?.name==='AbortError')return false;
      if(!current())return false;
      root.replaceChildren();
      const failure=document.createElement('div');failure.className='settings-message error';failure.textContent=aiProfileSafeError(error,'读取');root.appendChild(failure);
      aiProfileState.hasNext=false;
      setAIMemoryProfileMessage(aiProfileSafeError(error,'读取'),'error');
      return false;
    }finally{
      if(current())setAIMemoryProfileControls();
    }
  }
  async function commandAIMemoryProfile(target,action,value=null){
    if(aiProfileState.commandPending||!target?.key||!target?.memoryType||!AI_PROFILE_COMMANDS.has(action))return false;
    aiProfileState.commandPending=true;
    setAIMemoryProfileControls();
    setAIMemoryProfileMessage(`正在${AI_PROFILE_COMMAND_LABELS[action]}画像...`);
    try{
      await settingsAPI.aiMemoryProfileCommand({memory_type:target.memoryType,key:target.key,action,value});
      const refreshed=await loadAIMemoryProfile(aiProfileState.page,{announce:false});
      await loadAIMemory();
      setAIMemoryProfileMessage(
        refreshed?`画像已${AI_PROFILE_COMMAND_LABELS[action]}。`:`画像已${AI_PROFILE_COMMAND_LABELS[action]}，但列表刷新失败，请重新读取。`,
        refreshed?'success':'error'
      );
      return true;
    }catch(error){
      setAIMemoryProfileMessage(aiProfileSafeError(error,'操作'),'error');
      return false;
    }finally{
      aiProfileState.commandPending=false;
      setAIMemoryProfileControls();
    }
  }
  function currentAIMemoryProfileTarget(key){
    return {key,memoryType:document.getElementById('ai-memory-profile-type')?.value||'free'};
  }
  function openAIMemoryCorrection(key){
    aiProfileState.commandTarget={...currentAIMemoryProfileTarget(key),action:'correct'};
    showModal('纠正画像','请输入后续 AI 应采用的权威值。旧推断不会再同时进入上下文。','info',`<label for="ai-memory-correction-value" class="field-hint">权威画像值</label><input id="ai-memory-correction-value" class="settings-input" maxlength="500" autocomplete="off" required><div class="settings-actions"><button type="button" class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button type="button" class="btn btn-p btn-sm" onclick="applyAIMemoryCorrection()">保存纠正</button></div>`);
    window.setTimeout(()=>document.getElementById('ai-memory-correction-value')?.focus(),0);
  }
  async function applyAIMemoryCorrection(){
    const input=document.getElementById('ai-memory-correction-value');
    const value=input?.value.trim();
    if(!value){input?.setCustomValidity('纠正值不能为空。');input?.reportValidity();return}
    input.setCustomValidity('');
    const target=aiProfileState.commandTarget;hideModal();
    if(target)await commandAIMemoryProfile(target,'correct',value);
    aiProfileState.commandTarget=null;
  }
  function confirmAIMemoryProfileCommand(target,action){
    aiProfileState.commandTarget={...target,action};
    const label=AI_PROFILE_COMMAND_LABELS[action];
    const explanation=action==='reject'?'否定后，这条画像不会继续进入后续 AI 上下文。':'隐藏后，这条画像不会继续进入后续 AI 上下文，但仍可在“已隐藏”筛选中查看。';
    showModal(`${label}画像`,explanation,'warning',`<button type="button" class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button type="button" class="btn btn-d btn-sm" onclick="applyAIMemoryProfileCommand()">确认${label}</button>`);
  }
  async function applyAIMemoryProfileCommand(){
    const target=aiProfileState.commandTarget;hideModal();
    if(target)await commandAIMemoryProfile(target,target.action);
    aiProfileState.commandTarget=null;
  }
document.getElementById('ai-memory-profile-list')?.addEventListener('click',event=>{
  const button=event.target.closest('[data-ai-profile-action]');if(!button)return;
    const key=button.closest('[data-key]')?.dataset.key||'';const action=button.dataset.aiProfileAction;
    if(!key||!AI_PROFILE_COMMANDS.has(action))return;
    const target=currentAIMemoryProfileTarget(key);
    if(action==='correct')openAIMemoryCorrection(key);
    else if(action==='reject'||action==='suppress')confirmAIMemoryProfileCommand(target,action);
    else void commandAIMemoryProfile(target,action);
  });
  document.getElementById('ai-memory-profile-refresh')?.addEventListener('click',()=>loadAIMemoryProfile(1));
  document.getElementById('ai-memory-profile-status')?.addEventListener('change',()=>loadAIMemoryProfile(1));
  document.getElementById('ai-memory-profile-entry-type')?.addEventListener('change',()=>loadAIMemoryProfile(1));
  document.getElementById('ai-memory-profile-type')?.addEventListener('change',()=>loadAIMemoryProfile(1));
  document.getElementById('ai-memory-profile-prev')?.addEventListener('click',()=>loadAIMemoryProfile(Math.max(1,aiProfileState.page-1)));
  document.getElementById('ai-memory-profile-next')?.addEventListener('click',()=>loadAIMemoryProfile(aiProfileState.page+1));
  window.applyAIMemoryCorrection=applyAIMemoryCorrection;window.applyAIMemoryProfileCommand=applyAIMemoryProfileCommand;

function collectAISettings({onlyDirty=false,version=aiSettingsVersion}={}){
  const baseURL=document.getElementById('ai-base-url').value.trim();
  const key=document.getElementById('ai-api-key').value.trim();
  const error=document.getElementById('ai-base-url-error');
  error.textContent='';
  if(!baseURL){
    error.textContent='API 地址不能为空。';
    document.getElementById('ai-base-url').focus();
    return null;
  }
  if(baseURL){
    try{
      const parsed=new URL(baseURL);
      if(!['http:','https:'].includes(parsed.protocol))throw new Error();
    }catch(_error){
      error.textContent='请输入有效的 HTTP 或 HTTPS 地址。';
      document.getElementById('ai-base-url').focus();
      return null;
    }
  }
  const qualityModel=document.getElementById('ai-quality-model').value.trim();
  const fastModel=document.getElementById('ai-fast-model').value.trim();
  if(!qualityModel||!fastModel){
    setAISettingsStatus('高质量模型和快速模型都不能为空。','error');
    document.getElementById(!qualityModel?'ai-quality-model':'ai-fast-model').focus();
    return null;
  }
  const values=currentSettingsSnapshot();
  if(values.dns_mode==='domestic'){
    const servers=values.dns_servers.split(/[,，;；]/).map(value=>value.trim()).filter(Boolean);
    const ipv4=/^(?:\d{1,3}\.){3}\d{1,3}$/;
    if(!servers.length||servers.length>3||servers.some(value=>!ipv4.test(value))){
      setAISettingsStatus('国内 DNS 模式需填写 1 至 3 个 IPv4 DNS 地址。','error');
      document.getElementById('ai-dns-servers').focus();
      return null;
    }
  }
  const payload={
    active_profile:document.getElementById('ai-profile').value,
    provider:document.getElementById('ai-provider').value
  };
  for(const field of ['base_url','dns_mode','dns_servers','quality_model','fast_model','reasoning_effort']){
    if(!onlyDirty||aiSettingsDirty.has(field))payload[field]=values[field];
  }
  const assignments={};
  for(const field of ['classification','problem_analysis','daily_analysis','exam_generation']){
    if(!onlyDirty||aiSettingsDirty.has(field))assignments[field]=values[field];
  }
  if(Object.keys(assignments).length)payload.assignments=assignments;
  payload.api_key_action=key?'replace':'keep';
  if(key)payload.api_key=key;
  if(version!=null)payload.version=version;
  return payload;
}

async function submitAISettings(payload){
  return settingsAPI.request(settingsAPI.paths.ai,{method:'PUT',body:JSON.stringify(payload)});
}

async function rebaseAISettings(){
  const latestPayload=await settingsAPI.request(settingsAPI.paths.ai);
  const latest=normalizeAISettings(latestPayload);
  const latestValues=settingsSnapshot(latest);
  const localValues=currentSettingsSnapshot();
  const conflicts=[...aiSettingsDirty].filter(field=>
    field!=='api_key'&&
    aiSettingsBaseline&&
    String(latestValues[field])!==String(aiSettingsBaseline[field])&&
    String(localValues[field])!==String(latestValues[field])
  );
  if(!conflicts.length){
    aiSettingsVersion=latest.version;
    aiSettingsBaseline=latestValues;
    return submitAISettings(collectAISettings({onlyDirty:true,version:latest.version}));
  }
  pendingSettingsConflict={latestPayload,latest,latestValues,localValues,conflicts};
  const rows=conflicts.map(field=>{
    const label=document.querySelector(`label[for="${AI_SETTING_FIELDS[field]}"]`)?.textContent||field;
    return `<div><strong>${escapeHTML(label)}</strong><br>本地：${escapeHTML(localValues[field])}<br>最新：${escapeHTML(latestValues[field])}</div>`;
  }).join('<hr>');
  showRichModal(
    '设置冲突',
    `<p>以下字段在两端都被修改，请选择处理方式。</p>${rows}`,
    'error',
    '<button class="btn btn-out btn-sm" onclick="resolveAISettingsConflict(\'latest\')">使用最新值</button><button class="btn btn-p btn-sm" onclick="resolveAISettingsConflict(\'local\')">保留本地值</button>'
  );
  return null;
}

async function resolveAISettingsConflict(choice){
  const pending=pendingSettingsConflict;
  if(!pending)return;
  pendingSettingsConflict=null;
  hideModal();
  if(choice==='latest'){
    pending.conflicts.forEach(field=>{
      const input=document.getElementById(AI_SETTING_FIELDS[field]);
      if(input)input.value=pending.latestValues[field];
      aiSettingsDirty.delete(field);
    });
  }
  aiSettingsVersion=pending.latest.version;
  aiSettingsBaseline=pending.latestValues;
  const payload=collectAISettings({onlyDirty:true,version:pending.latest.version});
  try{
    const result=await submitAISettings(payload);
    applyAISettings(result?.settings||result);
    setAISettingsStatus('冲突已处理，设置已保存。','success');
  }catch(error){setAISettingsStatus(`冲突处理失败：${error.message}`,'error')}
}

async function testAISettings(){
  const button=document.getElementById('ai-test');
  button.disabled=true;
  setAISettingsStatus('正在测试连接...');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiTest,{method:'POST'});
    const latency=Number.isFinite(Number(result?.latency_ms))?`，${Number(result.latency_ms)} ms`:'';
    setAISettingsStatus(`连接成功${result?.model?`：${result.model}`:''}${latency}`,'success');
  }catch(error){
    setAISettingsStatus(`连接失败：${error.message}`,'error');
  }finally{button.disabled=false}
}

async function saveAISettings(event){
  event.preventDefault();
  const payload=collectAISettings({onlyDirty:true});
  if(!payload)return;
  const button=document.getElementById('ai-save');
  const credentialChanged=aiSettingsDirty.has('api_key');
  button.disabled=true;
  setAISettingsStatus('正在保存...');
  try{
    let result;
    try{result=await submitAISettings(payload)}
    catch(error){
      if(error.status===409&&error.data?.code==='SETTINGS_CONFLICT')result=await rebaseAISettings();
      else throw error;
    }
    if(!result)return;
    applyAISettings(result?.settings||result);
    if(credentialChanged)clearAIModelCatalog('密钥已更新，请重新读取模型');
    aiSettingsLoaded=true;
    setAISettingsStatus('设置已保存并立即生效。','success');
    if(settingsRouteActive)void loadSemanticIndexStatus({silent:true});
  }catch(error){
    const prefix=error.status===409?'设置冲突未能自动处理':'保存失败';
    setAISettingsStatus(`${prefix}：${error.message}`,'error');
  }finally{button.disabled=false}
}

document.getElementById('ai-settings-form')?.addEventListener('submit',saveAISettings);
Object.entries(AI_SETTING_FIELDS).forEach(([field,id])=>{
  document.getElementById(id)?.addEventListener('input',()=>aiSettingsDirty.add(field));
  document.getElementById(id)?.addEventListener('change',()=>aiSettingsDirty.add(field));
});
document.getElementById('ai-dns-mode')?.addEventListener('change',syncDNSSettingsState);
document.getElementById('ai-profile')?.addEventListener('change',event=>{
  const profileId=event.currentTarget.value;
  if(!aiSettingsPayload?.profiles?.[profileId])return;
  aiSettingsDirty.add('active_profile');
  for(const field of ['base_url','dns_mode','dns_servers','quality_model','fast_model','reasoning_effort'])aiSettingsDirty.add(field);
  applyAISettings({...aiSettingsPayload,active_profile:profileId},{resetDirty:false});
  clearAIModelCatalog('保存当前连接后可读取模型');
  setAISettingsStatus('已切换连接预览；保存后才会生效。','');
});
document.getElementById('ai-api-key')?.addEventListener('input',()=>aiSettingsDirty.add('api_key'));
document.getElementById('ai-models-load')?.addEventListener('click',loadAvailableAIModels);
document.getElementById('ai-models-list')?.addEventListener('click',refreshAIModelSelections);
document.getElementById('ai-fast-model')?.addEventListener('input',refreshAIModelSelections);
document.getElementById('ai-quality-model')?.addEventListener('input',refreshAIModelSelections);
document.querySelectorAll('[data-ai-speed]').forEach(button=>{
  button.addEventListener('click',()=>applyAISpeedPreference(button.dataset.aiSpeed));
});
document.querySelectorAll('[data-ai-effort]').forEach(button=>{
  button.addEventListener('click',()=>{
    const input=document.getElementById('ai-reasoning-effort');
    input.value=button.dataset.aiEffort||'';
    aiSettingsDirty.add('reasoning_effort');
    syncReasoningEffortButtons();
    setAISettingsStatus('思考程度已更新；保存后生效。');
  });
});
document.getElementById('ai-test')?.addEventListener('click',testAISettings);
document.getElementById('ai-migrate')?.addEventListener('click',async event=>{
  event.currentTarget.disabled=true;
  setAISettingsStatus('正在迁移旧配置...');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiMigrateLegacy,{method:'POST'});
    applyAISettings(result?.settings||result);
    event.currentTarget.hidden=true;
    setAISettingsStatus('旧配置已迁移。','success');
    if(settingsRouteActive)void loadSemanticIndexStatus({silent:true});
  }catch(error){setAISettingsStatus('迁移失败：'+error.message,'error')}
  finally{event.currentTarget.disabled=false}
});
document.getElementById('ai-key-toggle')?.addEventListener('click',event=>{
  const input=document.getElementById('ai-api-key');
  const show=input.type==='password';
  input.type=show?'text':'password';
  event.currentTarget.setAttribute('aria-pressed',String(show));
  event.currentTarget.setAttribute('aria-label',show?'隐藏当前输入的密钥':'显示当前输入的密钥');
});
document.getElementById('ai-reset')?.addEventListener('click',()=>{
  const label=document.getElementById('ai-profile')?.selectedOptions?.[0]?.textContent||'当前连接';
  showModal('恢复默认设置',`将恢复 ${label} 的默认地址、模型和任务分配；已保存凭据会保留。`,'info',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-p btn-sm" id="modal-confirm-btn" onclick="resetAISettings()">确认恢复</button>');
});
async function resetAISettings(){
  hideModal();setAISettingsStatus('正在恢复默认设置...');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiReset,{method:'POST',body:JSON.stringify({version:aiSettingsVersion})});
    applyAISettings(result);setAISettingsStatus('已恢复默认设置，凭据保持不变。','success');
  }catch(error){setAISettingsStatus(`恢复失败：${error.message}`,'error')}
}
document.getElementById('ai-delete-key')?.addEventListener('click',()=>{
  showModal('删除 AI 凭据','确定删除保存在 Windows 凭据管理器中的 AI 密钥吗？此操作不会修改其他设置。','error',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-d btn-sm" id="modal-confirm-btn" onclick="deleteAISettingsCredential()">确认删除</button>');
});
async function deleteAISettingsCredential(){
  hideModal();setAISettingsStatus('正在删除凭据...');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiCredential,{method:'DELETE',body:JSON.stringify({version:aiSettingsVersion,confirm:true})});
    applyAISettings(result);setAISettingsStatus('AI 凭据已删除。','success');
    if(settingsRouteActive)void loadSemanticIndexStatus({silent:true});
  }catch(error){setAISettingsStatus(`删除失败：${error.message}`,'error')}
}

window.loadAISettings=loadAISettings;
window.resolveAISettingsConflict=resolveAISettingsConflict;
window.resetAISettings=resetAISettings;
window.deleteAISettingsCredential=deleteAISettingsCredential;

async function loadRuntimeHealth(force=false){
  if(runtimeHealthLoaded&&!force)return;
  const button=document.getElementById('runtime-health-refresh');
  const status=document.getElementById('runtime-health-status');
  if(button)button.disabled=true;
  if(status)status.textContent='正在诊断...';
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.health(force));
    if(String(payload?.contract_version)!=='1')throw new Error('诊断契约版本不兼容');
    settingsRender.runtimeHealth(payload);
    runtimeHealthLoaded=true;
  }catch(error){
    if(status){
      status.textContent=`诊断失败：${error.message}`;
      status.className='settings-message error';
    }
  }finally{if(button)button.disabled=false}
}

document.getElementById('runtime-health-refresh')?.addEventListener('click',()=>loadRuntimeHealth(true));
window.loadRuntimeHealth=loadRuntimeHealth;

function clearSemanticStatusTimer(){
  if(semanticStatusTimer){window.clearTimeout(semanticStatusTimer);semanticStatusTimer=null}
}

function scheduleSemanticStatusRefresh(payload){
  clearSemanticStatusTimer();
  if(settingsRouteActive&&!document.hidden&&payload?.job_state?.semantic_active){
    semanticStatusTimer=window.setTimeout(()=>{
      semanticStatusTimer=null;
      void loadSemanticIndexStatus({silent:true});
    },1500);
  }
}

function syncSemanticFormState(){
  const provider=document.getElementById('semantic-provider-enabled');
  const auto=document.getElementById('semantic-auto-refresh');
  if(!provider||!auto)return;
  const providerState=semanticOperations?.settings_state?.provider||{};
  auto.disabled=!provider.checked||!providerState.credential_configured;
  auto.setAttribute('aria-disabled',String(auto.disabled));
  if(auto.disabled)auto.checked=false;
}

function semanticConfirmation(field,input){
  const copy={
    provider:{
      title:'启用语义分析',
      message:'语义分析会使用当前 AI 服务为题目生成材料卡，可能产生 Token、费用和较长的后台任务。确认启用吗？'
    },
    auto_refresh:{
      title:'启用题库发布后自动更新',
      message:'普通题库成功发布新索引后，系统会自动提交语义更新并可能产生 AI 用量。打开设置页本身不会提交任务。确认启用吗？'
    },
    allow_partial:{
      title:'允许使用部分语义结果',
      message:'索引未完整时将允许使用已经完成的部分，功能可更早恢复，但候选覆盖可能不完整。确认启用吗？'
    }
  }[field];
  input.checked=false;
  showModal(copy.title,copy.message,'info',
    '<button type="button" class="btn btn-out btn-sm" id="semantic-confirm-cancel">取消</button><button type="button" class="btn btn-p btn-sm" id="semantic-confirm-accept">确认启用</button>');
  document.getElementById('semantic-confirm-cancel')?.addEventListener('click',()=>hideModal());
  document.getElementById('semantic-confirm-accept')?.addEventListener('click',()=>{
    semanticConfirmations[field]=true;
    input.checked=true;
    hideModal();
    syncSemanticFormState();
  });
}

function bindSemanticCapabilityToggle(id,field){
  const input=document.getElementById(id);
  input?.addEventListener('change',()=>{
    if(!input.checked){
      semanticConfirmations[field]=false;
      syncSemanticFormState();
      return;
    }
    if(!semanticConfirmations[field]){
      semanticConfirmation(field,input);
      return;
    }
    syncSemanticFormState();
  });
}

bindSemanticCapabilityToggle('semantic-provider-enabled','provider');
bindSemanticCapabilityToggle('semantic-auto-refresh','auto_refresh');
bindSemanticCapabilityToggle('semantic-allow-partial','allow_partial');

async function loadSemanticIndexStatus(options={}){
  if(semanticStatusRequest)return semanticStatusRequest;
  const button=document.getElementById('semantic-index-refresh');
  const message=document.getElementById('semantic-index-message');
  if(button&&!options.silent)button.disabled=true;
  semanticStatusRequest=(async()=>{
    try{
      const payload=await settingsAPI.request(settingsAPI.paths.semanticOverview);
      if(payload?.schema!=='semantic-index-operations-v1')throw new Error('状态契约版本不兼容');
      if(settingsRouteActive){
        semanticOperations=settingsRender.semanticIndexStatus(payload);
        const activation=payload?.settings_state?.activation||{};
        semanticConfirmations={
          provider:Boolean(activation.provider),
          auto_refresh:Boolean(activation.auto_refresh),
          allow_partial:Boolean(activation.allow_partial)
        };
        syncSemanticFormState();
        scheduleSemanticStatusRefresh(payload);
      }
      return payload;
    }catch(error){
      if(semanticOperations?.job_state?.semantic_active) scheduleSemanticStatusRefresh(semanticOperations);
      else clearSemanticStatusTimer();
      if(message){message.textContent=`读取失败：${error.message}`;message.className='settings-message error'}
      return null;
    }finally{
      if(button&&!options.silent)button.disabled=false;
      semanticStatusRequest=null;
    }
  })();
  return semanticStatusRequest;
}
document.getElementById('semantic-index-refresh')?.addEventListener('click',()=>loadSemanticIndexStatus());
async function semanticWrite(path,payload){
  return settingsAPI.write(path,payload);
}
async function saveSemanticSettings(){
  const message=document.getElementById('semantic-index-message');
  if(!semanticOperations)return loadSemanticIndexStatus();
  const current=semanticOperations.settings;
  const activation={
    auto_refresh:document.getElementById('semantic-auto-refresh').checked&&semanticConfirmations.auto_refresh,
    allow_partial:document.getElementById('semantic-allow-partial').checked&&semanticConfirmations.allow_partial,
  };
  if(!semanticOperations?.settings_state?.provider?.managed){
    activation.provider=document.getElementById('semantic-provider-enabled').checked&&semanticConfirmations.provider;
  }
  const payload={version:semanticOperations.settings_revision,settings:{
    ...current,
    recall_mode:document.getElementById('semantic-recall-mode').value,
    max_concurrency:Number(document.getElementById('semantic-max-concurrency').value),
    candidate_multiplier:Number(document.getElementById('semantic-candidate-multiplier').value),
    auto_refresh_enabled:document.getElementById('semantic-auto-refresh').checked,
    allow_partial_cache:document.getElementById('semantic-allow-partial').checked,
  },activation};
  try{
    await settingsAPI.write(settingsAPI.paths.semanticIndex,payload,'PUT');
    await loadSemanticIndexStatus();
    message.textContent='语义索引设置已保存。';message.className='settings-message success';
  }catch(error){message.textContent=`保存失败：${error.message}`;message.className='settings-message error'}
}
async function runSemanticAction(action,{confirm=false}={}){
  const message=document.getElementById('semantic-index-message');
  const runId=semanticOperations?.latest_run?.id||null;
  try{
    const result=await semanticWrite(settingsAPI.paths.semanticAction(action),{
      version:semanticOperations?.settings_revision,run_id:runId,confirm
    });
    message.textContent=result.job_id?`任务已提交：${result.job_id}`:`操作已提交：${result.run_id}`;
    message.className='settings-message success';
    if(window.refreshJobs)window.refreshJobs();
    await loadSemanticIndexStatus();
  }catch(error){message.textContent=`操作失败：${error.message}`;message.className='settings-message error'}
}
document.getElementById('semantic-settings-save')?.addEventListener('click',saveSemanticSettings);
document.getElementById('semantic-action-refresh')?.addEventListener('click',()=>runSemanticAction('refresh'));
document.getElementById('semantic-action-pause')?.addEventListener('click',()=>runSemanticAction('pause'));
document.getElementById('semantic-action-resume')?.addEventListener('click',()=>runSemanticAction('resume'));
document.getElementById('semantic-action-rebuild')?.addEventListener('click',()=>{
  showModal('重建语义索引','重建会创建新的 run，不覆盖旧快照。确认提交吗？','error',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-d btn-sm" onclick="hideModal();runSemanticAction(\'rebuild\',{confirm:true})">确认重建</button>');
});
document.getElementById('semantic-cleanup-preview')?.addEventListener('click',async()=>{
  const message=document.getElementById('semantic-index-message');
  try{
    const preview=await settingsAPI.request(settingsAPI.paths.semanticCleanupPreview);
    const counts=preview.counts||{};
    message.textContent=`清理预览 ${preview.preview_hash.slice(0,12)}：卡 ${counts.superseded_cards||0}，部分快照 ${counts.partial_snapshots||0}，投影 ${counts.old_projections||0}，终态日志 ${counts.terminal_runs||0}。`;
    message.className='settings-message';
  }catch(error){message.textContent=`预览失败：${error.message}`;message.className='settings-message error'}
});
window.loadSemanticIndexStatus=loadSemanticIndexStatus;
window.runSemanticAction=runSemanticAction;

let questionBankStatusTimer=null;
let questionBankRebuildKey=null;

function scheduleQuestionBankStatusRefresh(rebuilding){
  if(questionBankStatusTimer){window.clearTimeout(questionBankStatusTimer);questionBankStatusTimer=null}
  if(rebuilding&&settingsRouteActive&&!document.hidden){
    questionBankStatusTimer=window.setTimeout(()=>{
      questionBankStatusTimer=null;
      void loadQuestionBankStatus({silent:true});
    },1500);
  }
}

async function loadQuestionBankStatus(options={}){
  const button=document.getElementById('question-bank-refresh');
  if(button&&!options.silent)button.disabled=true;
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.questionBankStatus);
    scheduleQuestionBankStatusRefresh(settingsRender.questionBankStatus(payload));
  }
  catch(error){const message=document.getElementById('question-bank-message');if(message){message.textContent=`读取失败：${error.message}`;message.className='settings-message error'}}
  finally{if(button&&!options.silent)button.disabled=false}
}

async function rebuildQuestionBank(){
  const button=document.getElementById('question-bank-rebuild');
  const message=document.getElementById('question-bank-message');
  if(button)button.disabled=true;
  try{
    questionBankRebuildKey=questionBankRebuildKey||(
      globalThis.crypto?.randomUUID?.()||`${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`
    );
    const result=await settingsAPI.request(settingsAPI.paths.questionBankRebuild,{
      method:'POST',headers:{'Idempotency-Key':questionBankRebuildKey}
    });
    window.trackJob?.(result.job||result);
    if(message){message.textContent=`重建任务已提交：${result.job_id}`;message.className='settings-message success'}
    window.refreshJobs?.();
    await loadQuestionBankStatus();
  }catch(error){if(message){message.textContent=`提交失败：${error.message}`;message.className='settings-message error'}}
  finally{if(button)button.disabled=false}
}

window.addEventListener('cuoti:job-update',event=>{
  const job=event.detail;
  if(job?.kind==='semantic-index-refresh'){
    if(settingsRouteActive)void loadSemanticIndexStatus({silent:true});
    return;
  }
  if(job?.kind!=='question-bank-rebuild')return;
  if(['succeeded','failed','cancelled'].includes(job.status)){
    questionBankRebuildKey=null;
    if(settingsRouteActive){
      void loadQuestionBankStatus({silent:true});
      void loadSemanticIndexStatus({silent:true});
    }
  }
});
document.getElementById('question-bank-refresh')?.addEventListener('click',loadQuestionBankStatus);
document.getElementById('question-bank-rebuild')?.addEventListener('click',rebuildQuestionBank);
window.loadQuestionBankStatus=loadQuestionBankStatus;

async function mountSettings(){
  settingsRouteActive=true;
  await Promise.all([loadAISettings(),loadRuntimeHealth(),loadQuestionBankStatus(),loadSemanticIndexStatus()]);
}

function unmountSettings(){
  settingsRouteActive=false;
  clearSemanticStatusTimer();
  if(questionBankStatusTimer){window.clearTimeout(questionBankStatusTimer);questionBankStatusTimer=null}
}

document.addEventListener('visibilitychange',()=>{
  if(!settingsRouteActive)return;
  if(document.hidden){
    clearSemanticStatusTimer();
    if(questionBankStatusTimer){window.clearTimeout(questionBankStatusTimer);questionBankStatusTimer=null}
    return;
  }
  void loadSemanticIndexStatus({silent:true});
  void loadQuestionBankStatus({silent:true});
});

window.Cuoti.features.register('settings',Object.freeze({
  mount:mountSettings,
  unmount:unmountSettings,
  refresh:mountSettings
}));
