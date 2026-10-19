(function initSettingsConnection(){
'use strict';
/* ① AI 服务：显式保存的表单。改动先累积在吸底保存条，保存时按版本号检测冲突（自动 rebase 或弹窗选择）。
   ② 模型分工经 saveAssignments 即改即存：只带已保存的服务商和分工，不夹带这里未保存的修改。 */
const settingsPage=window.Cuoti.settings;
const settingsState=settingsPage.state,settingsAPI=settingsPage.api,settingsRender=settingsPage.render,ui=settingsPage.ui;
const {h}=ui;
let aiSettingsLoaded=false,aiSettingsVersion=null,latestAIVersion=null,aiSettingsBaseline=null,aiSettingsPayload=null,pendingSettingsConflict=null,settingsSavePending=false,aiLoadError='';
const modelCatalogs=new Map();
const aiSettingsDirty=settingsState.dirtyFields;
const AI_SETTING_FIELDS=settingsRender.fields;
const normalizeAISettings=settingsRender.normalize;
const settingsSnapshot=settingsRender.snapshot;
const PROFILE_FIELDS=Object.freeze(['base_url','dns_mode','dns_servers','quality_model','fast_model','reasoning_effort']);
const FIELD_LABELS=Object.freeze({active_profile:'服务商',base_url:'接口地址',api_key:'API 密钥',fast_model:'快速模型',quality_model:'高质量模型',reasoning_effort:'思考程度',dns_mode:'网络连接',dns_servers:'DNS 服务器'});
const EFFORT_LABELS=Object.freeze({'':'自动',low:'低',medium:'中',high:'高',max:'极高'});
const ERROR_INPUTS=Object.freeze(['ai-base-url','ai-fast-model','ai-quality-model','ai-dns-servers']);
const el=id=>document.getElementById(id);
const value=id=>el(id)?.value??'';
function setValue(id,next){const node=el(id);if(node)node.value=next}

function setAISettingsStatus(message,tone=''){ui.setMessage(el('ai-settings-status'),message,tone)}
function setDirtyError(message){
  const text=el('ai-dirty-text');
  if(!text||!el('ai-foot')?.classList.contains('is-dirty')){setAISettingsStatus(message,'error');return}
  text.textContent=message;
  text.classList.toggle('is-error',true);
}

const savedSettings=()=>normalizeAISettings(aiSettingsPayload||{});
const profileView=profileId=>normalizeAISettings({...(aiSettingsPayload||{}),active_profile:profileId});
const draftProfileId=()=>value('ai-profile')||savedSettings().id;
const shortName=profileId=>settingsRender.PROFILE_META[profileId]?.short||savedSettings().profiles?.[profileId]?.name||profileId;
const profileName=profileId=>savedSettings().profiles?.[profileId]?.name||shortName(profileId);
/* 连接、密钥变化后，语义索引的可用性和运行状态里的“AI 配置”会跟着变，静默重读一次 */
function refreshDependents(){
  if(!settingsState.active)return;
  void settingsPage.maintenance?.loadSemanticIndexStatus?.({silent:true});
  void settingsPage.diagnostics?.loadRuntimeHealth?.({force:true,silent:true});
}

function currentSettingsSnapshot(){
  const values={};
  Object.entries(AI_SETTING_FIELDS).forEach(([field,id])=>{values[field]=el(id)?.value??''});
  return values;
}
function currentAssignments(){
  return Object.fromEntries(settingsRender.ROUTING_TASKS.map(task=>[task.key,value(AI_SETTING_FIELDS[task.key])||task.rec]));
}
const normalizeField=(field,raw)=>field==='dns_servers'?settingsRender.dnsList(raw).join(','):String(raw??'').trim();

/* 实际改动：同一服务商与基线比较；切换服务商时与那家已保存的值比较 */
function effectiveChanges(){
  if(!aiSettingsPayload)return [];
  const saved=savedSettings();
  const draft=draftProfileId();
  const switching=draft!==saved.id;
  const reference=switching?settingsSnapshot(profileView(draft)):(aiSettingsBaseline||settingsSnapshot(saved));
  const values=currentSettingsSnapshot();
  const changes=switching?['active_profile']:[];
  for(const field of PROFILE_FIELDS){
    if(normalizeField(field,values[field])!==normalizeField(field,reference[field]))changes.push(field);
  }
  if(value('ai-api-key').trim())changes.push('api_key');
  return changes;
}

function renderDirtyBar(changes){
  el('ai-foot')?.classList.toggle('is-dirty',changes.length>0);
  const text=el('ai-dirty-text');
  if(!text)return;
  text.classList.toggle('is-error',false);
  if(!changes.length){text.textContent='';return}
  const others=changes.filter(field=>field!=='active_profile');
  const labels=others.map(field=>FIELD_LABELS[field]||field).join('、');
  if(changes.includes('active_profile')){
    text.replaceChildren(`将改用“${shortName(draftProfileId())}”`,
      h('small',{text:others.length?`，另有 ${others.length} 项修改（${labels}）`:'，保存后生效'}));
  }else{
    text.replaceChildren(`有 ${others.length} 项修改未保存 `,h('small',{text:`（${labels}）`}));
  }
}

/* 用户改回原值时，从待保存集合里去掉；预览其他服务商时整组字段保持待保存，防止后台读取覆盖草稿 */
function refreshDirty(){
  const changes=effectiveChanges();
  if(!changes.includes('active_profile')){
    for(const field of [...aiSettingsDirty]){
      if((field==='active_profile'||field==='api_key'||PROFILE_FIELDS.includes(field))&&!changes.includes(field))aiSettingsDirty.delete(field);
    }
  }
  renderDirtyBar(changes);
  renderBadge();
  ui.changed();
  return changes;
}

function renderBadge(){
  if(!aiSettingsPayload){ui.setState(el('ai-state'),aiLoadError?{tone:'error',text:'读取失败'}:{tone:'',text:'正在读取…'});return}
  ui.setState(el('ai-state'),savedSettings().keyConfigured?{tone:'ok',text:'已就绪'}:{tone:'warn',text:'缺少密钥'});
}

function renderProviders(){
  const root=el('ai-providers');
  if(!root)return;
  const saved=savedSettings();
  const draft=draftProfileId();
  const profiles=saved.profiles||{};
  let ids=[...settingsRender.PROFILE_ORDER.filter(id=>profiles[id]),...Object.keys(profiles).filter(id=>!settingsRender.PROFILE_ORDER.includes(id))];
  if(!aiSettingsPayload)ids=[...settingsRender.PROFILE_ORDER];
  else if(!ids.length)ids=[saved.id];
  root.replaceChildren(...ids.map(id=>{
    const profile=profiles[id]||{};
    const meta=settingsRender.PROFILE_META[id]||{short:String(profile.name||id),mono:String(profile.name||id).slice(0,2).toUpperCase()};
    const checked=Boolean(aiSettingsPayload)&&id===draft;
    const hasKey=Boolean(profile.has_api_key??(id===saved.id&&saved.keyConfigured));
    const active=Boolean(aiSettingsPayload)&&id===saved.id;
    const note=!aiSettingsPayload?'读取中':active?(hasKey?'使用中 · 有密钥':'使用中 · 无密钥'):(hasKey?'已存密钥':'未配置');
    return h('button',{type:'button',class:'ai-provider',role:'radio','aria-checked':String(checked),tabindex:checked||(!aiSettingsPayload&&id===ids[0])?'0':'-1',
      dataset:{value:id},title:profile.name||meta.short,disabled:!aiSettingsPayload||null},
    h('span',{class:'ai-provider-mono','aria-hidden':'true',text:meta.mono}),
    h('strong',{text:meta.short}),
    h('small',{class:hasKey?'has-key':null},h('i'),note),
    h('span',{class:'ai-provider-check',html:ui.iconHTML('circleCheck')}));
  }));
}
const focusCheckedProvider=()=>el('ai-providers')?.querySelector('[aria-checked="true"]')?.focus({preventScroll:true});

function renderKeyState(){
  const node=el('ai-key-state');
  if(!node)return;
  const draft=profileView(draftProfileId());
  const typed=Boolean(value('ai-api-key').trim());
  const environment=settingsRender.environmentSource(draft.source);
  node.className=`set-hint ai-key-state ${!aiSettingsPayload?'':typed||draft.keyConfigured?'has-key':'no-key'}`;
  const text=typed?'保存后改用这个新密钥'
    :!aiSettingsPayload?'正在读取密钥状态…'
    :draft.keyConfigured?(environment?'由环境变量提供，这里不能修改'
      :draft.source==='legacy_file'?'保存在旧版配置文件里，建议迁移到凭据管理器'
      :'已保存在本机凭据管理器，留空即保持不变')
    :'还没有密钥，AI 功能暂不可用';
  node.replaceChildren(h('i'),text);
  const input=el('ai-api-key');
  if(input&&!input.disabled)input.placeholder=draft.keyConfigured?'留空则保持现有密钥':'粘贴服务商提供的密钥';
}

function renderUses(){
  const assignments=currentAssignments();
  for(const tier of ['fast','quality']){
    const node=el(`ai-${tier}-use`);
    if(!node)continue;
    const tasks=settingsRender.ROUTING_TASKS.filter(task=>assignments[task.key]===tier).map(task=>task.label);
    const parts=tasks.length?['用于：',h('b',{text:tasks.join('、')})]:['暂时没有任务用它，可在“模型分工”里调整'];
    const fast=value('ai-fast-model').trim();
    if(tier==='quality'&&fast&&fast===value('ai-quality-model').trim())parts.push(' · 与快速模型相同');
    node.replaceChildren(...parts);
  }
}

function renderNetwork(){
  const mode=value('ai-dns-mode')||'domestic';
  const servers=settingsRender.dnsList(value('ai-dns-servers'));
  const summary=el('ai-network-value');
  if(summary)summary.textContent=mode==='domestic'?`${settingsRender.DNS_LABELS.domestic} · ${servers.join(', ')||'未填写'}`:(settingsRender.DNS_LABELS[mode]||mode);
  const help=el('ai-dns-help');
  if(help)help.textContent=settingsRender.DNS_HELP[mode]||'';
  const field=el('ai-dns-servers-field');
  if(field)field.hidden=mode!=='domestic';
  const input=el('ai-dns-servers');
  if(input){input.disabled=mode!=='domestic';input.setAttribute('aria-disabled',String(mode!=='domestic'))}
  ui.setSeg(el('ai-dns-seg'),mode);
}

function renderNotes(){
  const payload=aiSettingsPayload||{};
  const legacy=el('ai-legacy-note');
  if(legacy)legacy.hidden=!(payload.legacy_detected||payload.can_migrate_legacy||payload.credential_source==='legacy_file');
  const custom=el('ai-provider-note');
  if(custom)custom.hidden=!aiSettingsPayload||draftProfileId()!=='custom';
}

function lockManagedFields(settings){
  const managed=settings.lockedFields.map(field=>String(field).toLowerCase());
  const environment=settingsRender.environmentSource(settings.source);
  const locked={};
  for(const [field,id] of Object.entries({base_url:'ai-base-url',api_key:'ai-api-key',quality_model:'ai-quality-model',fast_model:'ai-fast-model'})){
    locked[field]=managed.includes(field)||managed.some(name=>name.endsWith(field))||(environment&&field==='api_key');
    const input=el(id);
    if(!input)continue;
    input.disabled=locked[field];
    input.setAttribute('aria-disabled',String(locked[field]));
    if(locked[field])input.title='此项由环境变量管理';else input.removeAttribute('title');
  }
  document.querySelectorAll('.ai-slot-pick').forEach(button=>{button.disabled=Boolean(locked[`${button.dataset.pick}_model`])});
}

function renderAll(){
  renderProviders();renderKeyState();renderUses();renderNetwork();renderNotes();
  ui.setSeg(el('ai-effort'),value('ai-reasoning-effort'));
  refreshDirty();
}

function writeForm(settings,{secret=''}={}){
  setValue('ai-profile',settings.id);
  setValue('ai-provider',settings.adapter);
  setValue('ai-base-url',settings.baseURL);
  setValue('ai-dns-mode',settings.dnsMode);
  setValue('ai-dns-servers',settings.dnsServers);
  setValue('ai-quality-model',settings.qualityModel);
  setValue('ai-fast-model',settings.fastModel);
  setValue('ai-reasoning-effort',settings.reasoningEffort);
  setValue('ai-api-key',secret);
  lockManagedFields(settings);
}
function writeAssignments(settings){
  const assignmentValue=(name,fallback)=>settings.assignments?.[name]?.model_role||settings.assignments?.[name]||fallback;
  for(const task of settingsRender.ROUTING_TASKS)setValue(AI_SETTING_FIELDS[task.key],assignmentValue(task.key,task.rec));
}

function hideKey(){
  const input=el('ai-api-key'),button=el('ai-key-toggle');
  if(input)input.type='password';
  if(!button)return;
  button.setAttribute('aria-pressed','false');
  button.setAttribute('aria-label','显示当前输入的密钥');
  const slot=button.querySelector?.('[data-icon]');
  if(slot){slot.dataset.icon='eye';slot.innerHTML=ui.iconHTML('eye')}
}
function clearFieldErrors(){ERROR_INPUTS.forEach(id=>{const input=el(id);if(input)window.clearFieldError?.(input)})}
function showFieldErrors(errors){
  if(errors.some(([id])=>id==='ai-dns-servers')){const network=el('ai-network');if(network)network.open=true}
  if(typeof window.showFieldError!=='function'){setDirtyError(errors[0][1]);return}
  // 倒序显示，最后一次聚焦落在第一个错误上
  [...errors].reverse().forEach(([id,message])=>{const input=el(id);if(input)window.showFieldError(input,message)});
}

function applyAISettings(payload,{preserveSecret=false,resetDirty=true,submittedEdits=null}={}){
  const previousBaseline=aiSettingsBaseline,previousVersion=aiSettingsVersion;
  const retained=new Map();
  if(resetDirty){
    for(const field of aiSettingsDirty){
      if(submittedEdits&&submittedEdits.get(field)===settingsState.edits.get(field))continue;
      const input=el(field==='api_key'?'ai-api-key':AI_SETTING_FIELDS[field]);
      if(input)retained.set(field,input.value);
    }
  }
  const settings=normalizeAISettings(payload);
  const secret=preserveSecret?value('ai-api-key'):'';
  aiSettingsPayload=payload;
  aiSettingsVersion=settings.version;
  latestAIVersion=settings.version;
  aiLoadError='';
  writeForm(settings,{secret});
  writeAssignments(settings);
  if(resetDirty){
    aiSettingsBaseline=settingsSnapshot(settings);
    aiSettingsDirty.clear();
    for(const [field,fieldValue] of retained){
      setValue(field==='api_key'?'ai-api-key':AI_SETTING_FIELDS[field],fieldValue);
      aiSettingsDirty.add(field);
    }
    // An automatic read must not silently accept a new conflict baseline.
    if(!submittedEdits&&retained.size&&previousBaseline){
      aiSettingsBaseline=previousBaseline;aiSettingsVersion=previousVersion;
    }
    // 草稿里预览的是另一家服务商时，连接类型和受管字段要跟着草稿走
    const draft=draftProfileId();
    if(draft!==settings.id){
      const view=profileView(draft);
      setValue('ai-provider',view.adapter);
      lockManagedFields(view);
    }
  }
  settingsPage.routing?.afterApply?.();
  renderAll();
}

async function loadAISettings(force=false){
  if(settingsSavePending)return;
  if(aiSettingsLoaded&&!force)return;
  const operation=settingsAPI.beginOperation('loadAISettings');
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.ai);
    if(!operation.current())return;
    const recovering=Boolean(aiLoadError);
    applyAISettings(payload);
    aiSettingsLoaded=true;
    if(recovering)setAISettingsStatus('');
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;
    aiLoadError=error.status===404?'当前版本尚未提供设置接口。':`设置读取失败：${error.message}`;
    setAISettingsStatus(aiLoadError,'error');
    renderBadge();renderProviders();ui.changed();
  }
}

function collectAISettings({onlyDirty=false,version=aiSettingsVersion}={}){
  const baseURL=value('ai-base-url').trim();
  const key=value('ai-api-key').trim();
  const errors=[];
  clearFieldErrors();
  if(!baseURL)errors.push(['ai-base-url','请填写接口地址']);
  else{
    try{
      const parsed=new URL(baseURL);
      if(!['http:','https:'].includes(parsed.protocol))throw new Error();
    }catch(_error){
      errors.push(['ai-base-url','请填写以 http:// 或 https:// 开头的接口地址']);
    }
  }
  if(!value('ai-fast-model').trim())errors.push(['ai-fast-model','请填写快速模型名称，或从账号模型里选']);
  if(!value('ai-quality-model').trim())errors.push(['ai-quality-model','请填写高质量模型名称，或从账号模型里选']);
  const values=currentSettingsSnapshot();
  if(values.dns_mode==='domestic'){
    const servers=settingsRender.dnsList(values.dns_servers);
    if(!servers.length||servers.length>3||servers.some(server=>!settingsRender.isIPv4(server)))errors.push(['ai-dns-servers','请填写 1–3 个 IPv4 地址，用英文逗号分隔']);
    else values.dns_servers=servers.join(', ');
  }
  if(errors.length){showFieldErrors(errors);return null}
  values.base_url=baseURL;
  values.fast_model=values.fast_model.trim();
  values.quality_model=values.quality_model.trim();
  const payload={
    active_profile:value('ai-profile'),
    provider:value('ai-provider')
  };
  for(const field of PROFILE_FIELDS){
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

function displayValue(field,raw){
  const text=String(raw??'').trim();
  if(field==='active_profile')return profileName(text);
  if(field==='dns_mode')return settingsRender.DNS_LABELS[text]||text;
  if(field==='reasoning_effort')return EFFORT_LABELS[text]??text;
  return text||'（空）';
}

function showSettingsConflict({conflicts,localValues,latestValues}){
  showModal('设置冲突','','warning',
    '<button type="button" class="btn btn-out btn-sm" id="ai-conflict-latest">使用已保存的新值</button>'+
    '<button type="button" class="btn btn-p btn-sm" id="modal-confirm-btn">保留我的修改</button>');
  el('modal-body')?.replaceChildren(
    h('p',{class:'set-modal-text',text:'保存时发现下面几项在别处也被改过，请选择保留哪一边。'}),
    ...conflicts.map(field=>h('div',{class:'set-conflict-row'},
      h('strong',{text:FIELD_LABELS[field]||field}),
      h('span',{text:`你的修改：${displayValue(field,localValues[field])}`}),
      h('span',{text:`已保存的新值：${displayValue(field,latestValues[field])}`}))));
  el('ai-conflict-latest')?.addEventListener('click',()=>void resolveAISettingsConflict('latest'));
  el('modal-confirm-btn')?.addEventListener('click',()=>void resolveAISettingsConflict('local'));
  setDirtyError('保存未完成：请在弹窗里选择保留哪一边');
}

async function rebaseAISettings(){
  const operation=settingsAPI.beginOperation('rebaseAISettings');
  const latestPayload=await settingsAPI.request(settingsAPI.paths.ai);
  if(!operation.current())return null;
  const latest=normalizeAISettings(latestPayload);
  latestAIVersion=latest.version;
  const latestValues=settingsSnapshot(latest);
  const localValues=currentSettingsSnapshot();
  const conflicts=[...aiSettingsDirty].filter(field=>
    field!=='api_key'&&
    aiSettingsBaseline&&
    normalizeField(field,latestValues[field])!==normalizeField(field,aiSettingsBaseline[field])&&
    normalizeField(field,localValues[field])!==normalizeField(field,latestValues[field])
  );
  if(!conflicts.length){
    aiSettingsVersion=latest.version;
    aiSettingsBaseline=latestValues;
    const payload=collectAISettings({onlyDirty:true,version:latest.version});
    return payload?submitAISettings(payload):null;
  }
  pendingSettingsConflict={latestPayload,latest,latestValues,localValues,conflicts};
  showSettingsConflict(pendingSettingsConflict);
  return null;
}

async function resolveAISettingsConflict(choice){
  const operation=settingsAPI.beginOperation('resolveAISettingsConflict');
  const pending=pendingSettingsConflict;
  if(!pending)return;
  pendingSettingsConflict=null;
  hideModal();
  if(choice==='latest'){
    pending.conflicts.forEach(field=>{
      setValue(AI_SETTING_FIELDS[field],pending.latestValues[field]);
      aiSettingsDirty.delete(field);
    });
  }
  aiSettingsVersion=pending.latest.version;
  aiSettingsBaseline=pending.latestValues;
  const submittedEdits=new Map(settingsState.edits);
  const payload=collectAISettings({onlyDirty:true,version:pending.latest.version});
  if(!payload){refreshDirty();return}
  try{
    const result=await submitAISettings(payload);
    if(!operation.current())return;
    applyAISettings(result?.settings||result,{submittedEdits});
    modelCatalogs.clear();
    hideKey();
    setAISettingsStatus('冲突已处理，设置已保存。','ok');
    refreshDependents();
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;setDirtyError(`冲突处理失败：${error.message}`)}
}

const clockNow=()=>globalThis.performance?.now?.()??Date.now();
async function testAISettings(){
  const operation=settingsAPI.beginOperation('testAISettings');
  const button=el('ai-test');
  const saved=savedSettings();
  if(!aiSettingsPayload)return;
  if(!saved.keyConfigured){setAISettingsStatus('还没有保存密钥：先在上方填写并保存','error');return}
  setAISettingsStatus(`正在用 ${saved.fastModel||'快速模型'} 发一条很短的请求…`);
  const started=clockNow();
  try{
    const result=await ui.busy(button,'测试中…',()=>settingsAPI.request(settingsAPI.paths.aiTest,{method:'POST'}));
    if(!operation.current())return;
    const seconds=((clockNow()-started)/1000).toFixed(1);
    setAISettingsStatus(`连接正常 · ${result?.model||saved.fastModel} 用时 ${seconds} 秒`,'ok');
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;
    setAISettingsStatus(`连接失败：${error.message}`,'error');
  }
}

async function saveAISettings(event){
  event?.preventDefault?.();
  if(settingsSavePending)return;
  if(!effectiveChanges().length)return;
  const operation=settingsAPI.beginOperation('saveAISettings');
  const payload=collectAISettings({onlyDirty:true});
  if(!payload)return;
  const credentialChanged=aiSettingsDirty.has('api_key');
  const switched=payload.active_profile!==savedSettings().id;
  const submittedEdits=new Map(settingsState.edits);
  settingsAPI.cancelRead(settingsAPI.paths.ai);
  settingsSavePending=true;
  const restoreButton=ui.busyStart(el('ai-save'),'保存中…');
  const discard=el('ai-discard');
  if(discard)discard.disabled=true;
  try{
    let result;
    try{
      result=await submitAISettings(payload);
      if(!operation.current())return;
    }catch(error){
      if(!operation.current()||error?.name==='AbortError')return;
      if(error.status!==409||error.data?.code!=='SETTINGS_CONFLICT')throw error;
      result=await rebaseAISettings();
      if(!operation.current())return;
    }
    if(!result)return;
    applyAISettings(result?.settings||result,{submittedEdits});
    if(credentialChanged||switched)modelCatalogs.clear();
    aiSettingsLoaded=true;
    hideKey();
    setAISettingsStatus('已保存 · 之后新开始的任务生效','ok');
    el('ai-test')?.focus({preventScroll:true});
    window.showToast?.(switched?`AI 改用“${shortName(payload.active_profile)}”，之后新开始的任务生效。`:'AI 设置已保存，之后新开始的任务生效。',
      'success',{actionLabel:'测试连接',onAction:()=>void testAISettings()});
    refreshDependents();
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;
    const prefix=error.status===409?'设置冲突未能自动处理':'保存失败';
    setDirtyError(`${prefix}：${error.message}`);
  }finally{
    settingsSavePending=false;
    restoreButton();
    if(discard)discard.disabled=false;
    // 保存期间离开过本页时，新页面没有读取设置，这里补读一次
    if(settingsState.active&&!aiSettingsLoaded)void loadAISettings(true);
  }
}

/* 切换服务商只是预览：表单换成那家已保存的值，保存后才生效 */
function previewProfile(profileId){
  const saved=savedSettings();
  writeForm(profileView(profileId));
  clearFieldErrors();
  hideKey();
  aiSettingsDirty.delete('api_key');
  if(profileId===saved.id){
    for(const field of ['active_profile',...PROFILE_FIELDS])aiSettingsDirty.delete(field);
    aiSettingsBaseline=settingsSnapshot(saved);
    aiSettingsVersion=saved.version;
  }else{
    settingsState.markDirty('active_profile');
    for(const field of PROFILE_FIELDS)settingsState.markDirty(field);
  }
  renderAll();
}
async function selectProvider(profileId){
  const saved=savedSettings();
  if(!aiSettingsPayload||profileId===draftProfileId()){renderProviders();return}
  if(!saved.profiles?.[profileId]&&profileId!==saved.id){renderProviders();return}
  const edits=effectiveChanges().filter(field=>field!=='active_profile');
  if(edits.length){
    const ok=await ui.confirm({title:'切换服务商',type:'warning',danger:true,confirmLabel:'放弃并切换',
      body:`还有 ${edits.length} 项修改没保存（${edits.map(field=>FIELD_LABELS[field]||field).join('、')}），切换后会放弃这些修改。`});
    if(!ok){renderProviders();focusCheckedProvider();return}
  }
  previewProfile(profileId);
  focusCheckedProvider();
}

/* 账号模型目录：与接口要求一致，只能读取已保存的服务商，并且没有待保存的新密钥 */
function catalogBlocker(){
  const saved=savedSettings();
  if(!aiSettingsPayload)return '设置还在读取，请稍候。';
  if(draftProfileId()!==saved.id||value('ai-api-key').trim())return '先保存当前服务商和密钥，再读取账号里的模型。';
  if(!saved.keyConfigured)return '先保存这个服务商的密钥，才能读取账号里的模型。';
  return '';
}
async function fetchModelCatalog(){
  const saved=savedSettings();
  if(modelCatalogs.has(saved.id))return modelCatalogs.get(saved.id);
  const operation=settingsAPI.beginOperation('loadAvailableAIModels');
  const payload=await settingsAPI.request(settingsAPI.paths.aiModels,{method:'POST'});
  if(!operation.latest())throw new DOMException('Model catalog request superseded','AbortError');
  if(String(payload?.profile_id||'')!==saved.id)throw new Error('服务商已切换，请重新打开');
  modelCatalogs.set(saved.id,payload);
  return payload;
}
function withCatalog(node,{onMessage,onData}){
  const blocker=catalogBlocker();
  if(blocker){onMessage(blocker);return}
  const cached=modelCatalogs.get(savedSettings().id);
  if(cached){onData(cached);return}
  onMessage('正在读取账号里的模型…');
  fetchModelCatalog().then(data=>{if(node.isConnected)onData(data)},error=>{
    if(node.isConnected&&error?.name!=='AbortError')onMessage(`读取失败：${error.message}`);
  });
}
function chooseModel(slot,modelId){
  const input=el(`ai-${slot}-model`);
  if(!input||input.disabled)return;
  input.value=modelId;
  window.clearFieldError?.(input);
  settingsState.markDirty(`${slot}_model`);
  renderUses();refreshDirty();
}
function openPicker(trigger){
  const slot=trigger.dataset.pick==='quality'?'quality':'fast';
  const tierName=slot==='fast'?'快速':'高质量';
  ui.toggleMenu(trigger,(node,close)=>{
    const list=h('div',{class:'ai-picker-list',role:'listbox','aria-label':`选择${tierName}模型`});
    node.append(h('div',{class:'ai-picker-head'},h('span',{text:`${shortName(savedSettings().id)} 账号里的模型`})),list,
      h('div',{class:'ai-picker-foot',text:'也可以直接在输入框里填写模型名。'}));
    const message=text=>{list.replaceChildren(h('p',{class:'ai-picker-empty'},ui.iconNode('info'),text));ui.refreshMenu()};
    withCatalog(node,{onMessage:message,onData:data=>{
      const models=(Array.isArray(data?.models)?data.models:[]).filter(model=>String(model?.id||'').trim());
      if(!models.length){message('这个服务商没有返回模型列表，请直接填写模型名。');return}
      const current=value(`ai-${slot}-model`).trim();
      const recommended=data?.recommendations?.[slot];
      list.replaceChildren(...models.map(model=>{
        const id=String(model.id).trim();
        const selected=id===current;
        const tier=Object.hasOwn(settingsRender.TIER_LABELS,model.speed_tier)?model.speed_tier:'balanced';
        return h('button',{type:'button',class:'ai-picker-item',role:'option','aria-selected':String(selected),
          onclick:()=>{chooseModel(slot,id);close()}},
        h('span',null,h('strong',{text:`${model.display_name||id}${recommended===id?' · 推荐':''}`}),h('code',{text:id})),
        h('span',{class:`ai-tier ai-tier-${tier}`,text:settingsRender.TIER_LABELS[tier]}),
        h('span',{html:selected?ui.iconHTML('check'):''}));
      }));
      ui.refreshMenu();
      (list.querySelector('[aria-selected="true"]')||list.querySelector('button'))?.focus({preventScroll:true});
    }});
  },{className:'ai-picker',role:'dialog',label:`从账号模型选择${tierName}模型`});
}
/* 推荐搭配：沿用原来“速度偏好”的三种组合，收进一个菜单 */
function openPresets(trigger){
  ui.toggleMenu(trigger,node=>{
    const title=h('div',{class:'set-menu-title'});
    node.append(title);
    withCatalog(node,{onMessage:text=>{title.textContent=text;ui.refreshMenu()},onData:data=>{
      const rec=data?.recommendations||{};
      if(!rec.fast||!rec.balanced||!rec.quality){title.textContent='这个服务商没有提供推荐搭配，请直接填写两个模型名。';ui.refreshMenu();return}
      title.textContent='按账号模型一键填好两档，保存后生效';
      const apply=(fast,quality)=>{chooseModel('fast',fast);chooseModel('quality',quality)};
      node.append(
        ui.menuItem({icon:'zap',label:'速度优先',hint:`${rec.fast} + ${rec.fast}`,onClick:()=>apply(rec.fast,rec.fast)}),
        ui.menuItem({icon:'sliders',label:'均衡',hint:`${rec.fast} + ${rec.balanced}`,onClick:()=>apply(rec.fast,rec.balanced)}),
        ui.menuItem({icon:'sparkle',label:'质量优先',hint:`${rec.balanced} + ${rec.quality}`,onClick:()=>apply(rec.balanced,rec.quality)}));
      ui.refreshMenu();
      node.querySelector('button')?.focus({preventScroll:true});
    }});
  },{label:'推荐搭配'});
}

/* 恢复默认：服务端立即生效，同时重置模型分工；密钥保留 */
async function resetAISettings(){
  const operation=settingsAPI.beginOperation('resetAISettings');
  settingsAPI.cancelRead(settingsAPI.paths.ai);
  setAISettingsStatus('正在恢复默认设置…');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiReset,{method:'POST',body:JSON.stringify({version:latestAIVersion})});
    if(!operation.current())return;
    aiSettingsDirty.clear();
    applyAISettings(result);
    clearFieldErrors();hideKey();
    modelCatalogs.clear();
    setAISettingsStatus('已恢复默认设置，密钥保持不变。','ok');
    refreshDependents();
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;setAISettingsStatus(`恢复失败：${error.message}`,'error')}
}
async function confirmReset(){
  const pending=effectiveChanges().length;
  const ok=await ui.confirm({title:'恢复默认设置',type:'info',confirmLabel:'确认恢复',body:[
    `将恢复“${shortName(savedSettings().id)}”的默认地址、模型、网络设置和模型分工，立即生效；已保存的密钥会保留。`,
    pending?'页面上还没保存的修改也会一起放弃。':''
  ]});
  if(ok)await resetAISettings();
}
async function deleteAISettingsCredential(){
  const operation=settingsAPI.beginOperation('deleteAISettingsCredential');
  settingsAPI.cancelRead(settingsAPI.paths.ai);
  setAISettingsStatus('正在删除密钥…');
  try{
    const result=await settingsAPI.request(settingsAPI.paths.aiCredential,{method:'DELETE',body:JSON.stringify({version:latestAIVersion,confirm:true})});
    if(!operation.current())return;
    applyAISettings(result);
    modelCatalogs.clear();
    setAISettingsStatus('密钥已删除。','ok');
    refreshDependents();
  }catch(error){if(!operation.current()||error?.name==='AbortError')return;setAISettingsStatus(`删除失败：${error.message}`,'error')}
}
async function confirmDeleteKey(){
  const ok=await ui.confirm({title:'删除已保存密钥',type:'error',danger:true,confirmLabel:'删除密钥',body:[
    `将删除保存在 Windows 凭据管理器中的“${shortName(savedSettings().id)}”密钥，其他设置不变。`,
    '删除后 AI 功能不可用，直到重新填写并保存密钥。'
  ]});
  if(ok)await deleteAISettingsCredential();
}
async function migrateLegacy(event){
  const button=event.currentTarget,operation=settingsAPI.beginOperation('migrateAISettings');
  const submittedEdits=new Map(settingsState.edits);
  settingsAPI.cancelRead(settingsAPI.paths.ai);
  try{
    const result=await ui.busy(button,'迁移中…',()=>settingsAPI.request(settingsAPI.paths.aiMigrateLegacy,{method:'POST'}));
    if(!operation.current())return;
    applyAISettings(result?.settings||result,{submittedEdits});
    setAISettingsStatus('旧配置已迁移到 Windows 凭据管理器。','ok');
    refreshDependents();
  }catch(error){if(operation.current()&&error?.name!=='AbortError')setAISettingsStatus('迁移失败：'+error.message,'error')}
}
function discardChanges(){
  if(settingsSavePending||!aiSettingsPayload)return;
  const saved=savedSettings();
  aiSettingsDirty.clear();
  aiSettingsBaseline=settingsSnapshot(saved);
  aiSettingsVersion=saved.version;
  writeForm(saved);
  clearFieldErrors();hideKey();
  renderAll();
  el('ai-test')?.focus({preventScroll:true});
}

/* ② 模型分工的即改即存：版本冲突时取最新版本重试一次 */
async function saveAssignments(assignments){
  if(!aiSettingsPayload)throw new Error('设置还在读取，请稍候');
  const attempt=version=>settingsAPI.request(settingsAPI.paths.ai,{method:'PUT',body:JSON.stringify({
    active_profile:savedSettings().id,assignments,api_key_action:'keep',version
  })});
  let result;
  try{
    result=await attempt(latestAIVersion);
  }catch(error){
    if(error?.name==='AbortError'||error.status!==409||error.data?.code!=='SETTINGS_CONFLICT')throw error;
    const latest=await settingsAPI.request(settingsAPI.paths.ai);
    applyAISettings(latest);
    result=await attempt(latestAIVersion);
  }
  applyAISettings(result?.settings||result);
  return result;
}
function reapplySavedAssignments(){
  writeAssignments(savedSettings());
  renderUses();
}

const settingsForm=el('ai-settings-form');
settingsForm?.addEventListener('submit',saveAISettings);
for(const [field,id] of [['base_url','ai-base-url'],['fast_model','ai-fast-model'],['quality_model','ai-quality-model'],['dns_servers','ai-dns-servers']]){
  el(id)?.addEventListener('input',()=>{
    settingsState.markDirty(field);
    if(field.endsWith('_model'))renderUses();
    if(field==='dns_servers')renderNetwork();
    refreshDirty();
  });
}
el('ai-api-key')?.addEventListener('input',()=>{settingsState.markDirty('api_key');renderKeyState();refreshDirty()});
ui.bindSeg(el('ai-providers'),profileId=>void selectProvider(profileId));
ui.bindSeg(el('ai-effort'),effort=>{
  setValue('ai-reasoning-effort',effort);
  settingsState.markDirty('reasoning_effort');
  refreshDirty();
});
ui.bindSeg(el('ai-dns-seg'),mode=>{
  setValue('ai-dns-mode',mode);
  settingsState.markDirty('dns_mode');
  const servers=el('ai-dns-servers');
  if(servers)window.clearFieldError?.(servers);
  renderNetwork();refreshDirty();
});
document.querySelectorAll('.ai-slot-pick').forEach(trigger=>trigger.addEventListener('click',()=>openPicker(trigger)));
el('ai-preset-open')?.addEventListener('click',event=>openPresets(event.currentTarget));
el('ai-test')?.addEventListener('click',()=>void testAISettings());
el('ai-migrate')?.addEventListener('click',migrateLegacy);
el('ai-discard')?.addEventListener('click',discardChanges);
el('ai-more')?.addEventListener('click',event=>ui.toggleMenu(event.currentTarget,node=>{
  const saved=savedSettings();
  const environment=settingsRender.environmentSource(saved.source);
  node.append(
    ui.menuItem({icon:'rotateCcw',label:'恢复默认设置',hint:'恢复默认地址、模型和模型分工，保留密钥',disabled:!aiSettingsPayload,onClick:()=>void confirmReset()}),
    h('hr'),
    ui.menuItem({icon:'trash',label:'删除已保存密钥',danger:true,disabled:!saved.keyConfigured||environment,
      hint:environment?'密钥由环境变量提供，不能在这里删除':saved.keyConfigured?'删除后 AI 功能不可用，直到重新填写':'这个服务商还没有保存密钥',
      onClick:()=>void confirmDeleteKey()}));
},{label:'AI 服务：更多操作'}));
el('ai-key-toggle')?.addEventListener('click',event=>{
  const input=el('ai-api-key');
  if(!input)return;
  const show=input.type==='password';
  input.type=show?'text':'password';
  const slot=event.currentTarget.querySelector('[data-icon]');
  if(slot){slot.dataset.icon=show?'eyeOff':'eye';slot.innerHTML=ui.iconHTML(slot.dataset.icon)}
  event.currentTarget.setAttribute('aria-pressed',String(show));
  event.currentTarget.setAttribute('aria-label',show?'隐藏当前输入的密钥':'显示当前输入的密钥');
});

function status(){
  if(!aiSettingsPayload)return aiLoadError?{tone:'error',text:'读取失败'}:{tone:'',text:'正在读取'};
  if(effectiveChanges().length)return {tone:'warn',text:'有未保存修改'};
  const saved=savedSettings();
  return saved.keyConfigured?{tone:'ok',text:`${shortName(saved.id)} · 已就绪`}:{tone:'warn',text:'缺少密钥'};
}
function savedModels(){
  const saved=savedSettings();
  return {loaded:Boolean(aiSettingsPayload),fast:saved.fastModel,quality:saved.qualityModel,keyConfigured:saved.keyConfigured};
}

window.loadAISettings=loadAISettings;
window.resolveAISettingsConflict=resolveAISettingsConflict;
window.resetAISettings=resetAISettings;
window.deleteAISettingsCredential=deleteAISettingsCredential;

function mount(){
  const save=el('ai-save');
  if(save)save.disabled=settingsSavePending;
  for(const id of ['ai-test','ai-migrate']){const button=el(id);if(button)button.disabled=false}
  renderAll();
  return loadAISettings();
}
function unmount(){aiSettingsLoaded=false;pendingSettingsConflict=null;}
settingsPage.connection=Object.freeze({mount,unmount,loadAISettings,saveAISettings,saveAssignments,reapplySavedAssignments,savedModels,status,
  refreshUses:renderUses,isLoaded:()=>Boolean(aiSettingsPayload)});

})();
