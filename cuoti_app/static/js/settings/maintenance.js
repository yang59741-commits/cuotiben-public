(function initSettingsMaintenance(){
'use strict';
/* ⑤ 普通题库索引与 ⑥ 语义索引。题库只有“后台重建”一个主操作；语义索引按当前状态只给一个主按钮，
   其余设置即改即存（串行保存，版本冲突时读回最新版本重试一次）。 */
const settingsPage=window.Cuoti.settings;
const settingsState=settingsPage.state,settingsAPI=settingsPage.api,settingsRender=settingsPage.render,ui=settingsPage.ui;
const {h,term}=ui;
const {fmt,timeLabel,semanticReasonLabel,semanticRunLabel}=settingsRender;
const el=id=>document.getElementById(id);
const num=value=>{const number=Number(value);return Number.isFinite(number)?number:0};
const TERMINAL_JOBS=new Set(['succeeded','failed','cancelled']);
function definitions(node,rows){
  node?.replaceChildren(...rows.filter(Boolean).flatMap(([name,value])=>[h('dt',{text:name}),h('dd',null,value)]));
}
/* 两张卡互相引用对方的状态（题库详情里的“接力”、语义索引的自动更新），所以状态放在最前面 */
let overview=null,overviewError='';

/* ── ⑤ 普通题库索引 ── */
let bank=null,bankError='',bankNote=null,bankJob=null,bankPollTimer=0,rebuildKey=null,rebuildSubmitting=false;

function bankStatus(){
  if(!bank)return bankError?{tone:'error',text:'不可用'}:{tone:'',text:'尚未检查'};
  if(bank.rebuilding)return {tone:'busy',text:'重建中'};
  if(!bank.available)return {tone:'error',text:'不可用'};
  if(bank.stale)return {tone:'warn',text:'待重建'};
  return {tone:'ok',text:'可用'};
}

function figure(value,unit,label){
  return h('div',{class:'bank-figure'},h('strong',null,value,unit?h('small',{text:unit}):null),h('span',{text:label}));
}

function relayText(relay){
  if(!relay||typeof relay!=='object')return '';
  const status=String(relay.status||'');
  if(status==='submitted')return '已接着提交语义索引更新。';
  if(status==='replayed')return '语义索引更新已在进行。';
  if(status==='skipped'){
    if(relay.reason==='disabled')return '';
    if(relay.reason==='snapshot_already_indexed')return '语义索引已对应这份题库，无需重复更新。';
    return `语义索引没有自动更新：${semanticReasonLabel(relay.reason)}。`;
  }
  if(status==='deferred')return `语义索引自动更新暂缓：${semanticReasonLabel(relay.reason)}。`;
  return '';
}
const jobError=job=>(typeof job?.error==='string'?job.error:job?.error?.message)||job?.error_message||'';

function bankMessage(){
  if(bank?.rebuilding)return {text:bankJob?.stage||'在后台进行，可以离开本页；新索引检查通过后才替换。',tone:''};
  if(bankNote)return bankNote;
  if(!bank)return bankError?{text:`读取失败：${bankError}`,tone:'error'}:{text:'',tone:''};
  if(bank.error)return {text:`最近一次重建失败：${bank.error}`,tone:'error'};
  if(!bank.available)return {text:'还没有可用的索引，点“后台重建”生成。',tone:'error'};
  if(bank.stale)return {text:'题库文件改过了。重建后组卷才能用上新内容，在此之前旧索引照常可用。',tone:''};
  return {text:'',tone:''};
}

function renderRebuildButton(){
  const button=el('question-bank-rebuild');
  if(!button||rebuildSubmitting)return;
  const rebuilding=Boolean(bank?.rebuilding);
  button.className=`btn btn-sm ${bank&&(bank.stale||!bank.available)&&!rebuilding?'btn-p':'btn-out'}`;
  button.disabled=rebuilding;
  button.replaceChildren(ui.iconNode(rebuilding?'loader':'refresh',rebuilding?'icon-spin':''),rebuilding?'重建中…':'后台重建');
}

function renderBank(){
  ui.setState(el('question-bank-state'),bankStatus());
  const coverage=bank?.coverage||{},sources=bank?.sources||{};
  el('bank-figures')?.replaceChildren(...(bank?[
    figure(fmt(coverage.total_questions),'道','题目'),
    figure(`${fmt(sources.available)}/${fmt(sources.configured)}`,'','题源可用'),
    figure(timeLabel(bank.last_success_at,'尚无'),'','最近一次重建')
  ]:[]));
  renderRebuildButton();
  const message=bankMessage();
  ui.setMessage(el('question-bank-message'),message.text,message.tone);
  const progress=el('bank-progress');
  if(progress){
    const steps=bankJob&&num(bankJob.totalSteps)>0?Math.min(1,num(bankJob.completedSteps)/num(bankJob.totalSteps)):null;
    progress.hidden=!bank?.rebuilding;
    progress.classList.toggle('is-indeterminate',steps==null);
    progress.firstElementChild?.style?.setProperty?.('width',steps==null?'':`${Math.max(6,steps*100)}%`);
  }
  if(!bank){el('question-bank-details')?.replaceChildren();return}
  const relayOn=Boolean(overview?.settings_state?.effective?.auto_refresh_enabled);
  definitions(el('question-bank-details'),[
    ['最近可用版本',bank.last_good_snapshot?h('code',{text:String(bank.last_good_snapshot).slice(0,12)}):'无'],
    ['题源',`${fmt(sources.available)} 个可用 / 共 ${fmt(sources.configured)} 个${num(sources.missing)?`，缺失 ${fmt(sources.missing)} 个`:''}`],
    ['题目数',`${fmt(coverage.total_questions)} 道`],
    ['最近一次重建',timeLabel(bank.last_success_at,'尚无')],
    bank.latest_job?['最近任务',`${{queued:'等待中',running:'进行中',cancel_requested:'正在取消',succeeded:'已完成',failed:'失败',cancelled:'已取消'}[bank.latest_job.status]||String(bank.latest_job.status||'未知')} · ${timeLabel(bank.latest_job.updated_at,'—')}`]:null,
    ['接力语义索引',relayOn?'重建完成后自动提交语义更新':'未开启（在语义索引里打开“题库更新后自动更新”）']
  ]);
}

function scheduleBankPoll(){
  ui.cancel(bankPollTimer);bankPollTimer=0;
  if(bank?.rebuilding&&settingsState.active&&!document.hidden){
    bankPollTimer=ui.later(()=>{bankPollTimer=0;void loadQuestionBankStatus({silent:true})},1500);
  }
}

async function loadQuestionBankStatus(options={}){
  const operation=settingsAPI.beginOperation('loadQuestionBankStatus');
  const button=el('question-bank-refresh');
  if(!options.silent)ui.spinning(button,true);
  try{
    const payload=await settingsAPI.request(settingsAPI.paths.questionBankStatus);
    if(!operation.current())return;
    const wasRebuilding=Boolean(bank?.rebuilding);
    bank=payload&&typeof payload==='object'?payload:{};
    bankError='';
    if(bank.rebuilding)bankNote=null;
    else if(wasRebuilding)bankJob=null;
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    bank=null;
    bankError=error.message||'未知错误';
  }finally{
    if(operation.current()){
      ui.spinning(button,false);
      renderBank();scheduleBankPoll();ui.changed();
    }
  }
}

async function rebuildQuestionBank(){
  if(rebuildSubmitting)return;
  const operation=settingsAPI.beginOperation('rebuildQuestionBank');
  const restore=ui.busyStart(el('question-bank-rebuild'),'提交中…');
  rebuildSubmitting=true;
  bankNote=null;
  try{
    rebuildKey=rebuildKey||(globalThis.crypto?.randomUUID?.()||`${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`);
    const result=await settingsAPI.request(settingsAPI.paths.questionBankRebuild,{method:'POST',headers:{'Idempotency-Key':rebuildKey}});
    if(!operation.current())return;
    window.trackJob?.(result?.job||result);
    window.refreshJobs?.();
    if(bank)bank.rebuilding=true;
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    bankNote={text:`提交失败：${error.message}`,tone:'error'};
  }finally{
    rebuildSubmitting=false;
    restore();
    if(operation.current()){
      renderBank();ui.changed();
      void loadQuestionBankStatus({silent:true});
    }
  }
}

/* ── ⑥ 语义索引：状态 ── */
const RUNNING_RUNS=new Set(['scanning','queued','running','cancel_requested']);
const LOCKED_RUNS=new Set(['paused_budget_review','paused_token_hard_limit','outcome_unknown']);
const MODE_HINTS=Object.freeze({
  off:()=>'组卷时不使用语义索引。',
  shadow:()=>'只在后台记录“如果用了会怎样”，不影响组卷结果。适合先观察一段时间。',
  enforced:view=>`按题意参与候选题排序，会改变组卷结果。${view&&view.ratio<.95&&view.eligible?`当前覆盖 ${Math.floor(view.ratio*100)}%，没有材料卡的题按原方式排序。`:''}`
});
let statusRequest=null,semanticPollTimer=0,primaryKey='',actionPending='',actionMessage=null;
/* 即改即存：保存完成前把用户刚改的值叠加在读回的状态上，界面不会闪回旧值 */
const pendingSettings=new Map();
let pendingProvider=null;
const savingControls=new Set();

function overlay(payload){
  if(!payload||typeof payload!=='object')return payload;
  if(pendingSettings.size)payload.settings={...(payload.settings||{}),...Object.fromEntries(pendingSettings)};
  if(pendingProvider!=null){
    const state=payload.settings_state||{};
    payload.settings_state={...state,provider:{...(state.provider||{}),requested:pendingProvider}};
  }
  return payload;
}

function semanticView(){
  if(!overview)return null;
  const state=overview.settings_state||{},provider=state.provider||{},counts=overview.counts||{};
  const run=overview.latest_run&&typeof overview.latest_run==='object'?overview.latest_run:null;
  const runStatus=String(run?.status||'');
  const eligible=num(counts.eligible),valid=num(counts.valid);
  const ratio=overview.coverage&&overview.coverage.ratio!=null?num(overview.coverage.ratio):(eligible?valid/eligible:0);
  return {settings:overview.settings||{},state,provider,counts,run,runStatus,eligible,valid,ratio,
    requested:Boolean(provider.requested),effective:Boolean(provider.effective),credential:Boolean(provider.credential_configured),
    managed:Boolean(provider.managed),active:Boolean(overview.job_state?.semantic_active)||RUNNING_RUNS.has(runStatus),
    total:num(run?.pending_count),done:num(run?.completed_count)+num(run?.failed_count),
    pending:num(counts.stale)+num(counts.missing),snapshot:String(overview.semantic_snapshot?.status||'missing'),
    capabilities:overview.capabilities||{},autoEffective:Boolean(state.effective?.auto_refresh_enabled)};
}

function semanticStatus(){
  const view=semanticView();
  if(!view)return overviewError?{tone:'error',text:'读取失败'}:{tone:'',text:'正在读取…'};
  if(!view.requested)return {tone:'',text:'未启用'};
  if(!view.credential)return {tone:'warn',text:'缺少 AI 密钥'};
  if(!view.effective)return {tone:'warn',text:'暂不可用'};
  if(view.active)return {tone:'busy',text:view.total?`更新中 ${Math.floor(Math.min(1,view.done/view.total)*100)}%`:'更新中'};
  if(view.runStatus==='paused')return {tone:'warn',text:'已暂停'};
  if(LOCKED_RUNS.has(view.runStatus))return {tone:'warn',text:'待人工复核'};
  if(view.snapshot==='missing')return {tone:'warn',text:'尚未建立'};
  if(view.pending)return {tone:'warn',text:`待更新 ${fmt(view.pending)} 道`};
  return {tone:'ok',text:'已是最新'};
}

/* 用量只按上一次运行的实际消耗估算，样本太少时不给数字 */
function estimateTokens(count){
  const done=num(overview?.latest_run?.completed_count),used=num(overview?.tokens?.consumed);
  if(done<5||!used||!count)return '';
  return settingsRender.wanTokens(used/done*count);
}

/* ── ⑥ 语义索引：渲染 ── */
function renderCoverage(view){
  const root=el('sem-coverage');
  if(!root)return;
  if(!view){root.replaceChildren();return}
  if(view.snapshot==='missing'&&!view.eligible){
    root.replaceChildren(h('p',{class:'set-hint',text:'还没有建立语义索引：启用语义分析后，AI 会为每道题写一张简短的材料卡。'}));
    return;
  }
  const counts=view.counts;
  const parts=[['valid','可用',num(counts.valid),''],['stale','过期',num(counts.stale),'题目改过，需要重新分析'],
    ['missing','缺失',num(counts.missing),'还没有材料卡'],['excluded','排除',num(counts.excluded),'按规则不参与索引']];
  root.replaceChildren(
    h('div',{class:'sem-coverage-top'},h('strong',null,String(Math.floor(Math.min(1,view.ratio)*100)),h('small',{text:'%'})),
      h('span',null,`${fmt(view.valid)} / ${fmt(view.eligible)} 道题已有可用`,term('材料卡','AI 读题后写下的简短说明：考什么知识点、题意是什么'))),
    h('div',{class:'sem-bar',role:'img','aria-label':parts.map(([,label,value])=>`${label} ${value} 道`).join('，')},
      ...parts.filter(([,,value])=>value>0).map(([key,label,value])=>h('i',{class:`k-${key}`,style:`flex:${value} 1 0`,title:`${label} ${fmt(value)} 道`}))),
    h('div',{class:'sem-legend'},...parts.map(([key,label,value,tip])=>h('span',null,h('i',{class:`k-${key}`}),tip?term(label,tip):label,h('b',{text:fmt(value)})))));
}

const spotlightAI=()=>ui.spotlight('#ai-api-key');
function primaryPlan(view){
  if(!view)return {key:'none',text:''};
  const caps=view.capabilities;
  if(!view.requested){
    if(view.managed)return {key:'managed-off',text:'语义分析已被启动配置关闭，这里不能开启。'};
    if(!view.credential)return {key:'need-ai',text:'语义分析需要 AI 服务里有可用的密钥和模型。',button:{label:'去配置 AI 服务',icon:'arrowRight',quiet:true,onClick:spotlightAI}};
    return {key:'enable',text:'启用后才会生成材料卡；更新时会产生 AI 用量。',button:{label:'启用语义分析',icon:'sparkles',onClick:()=>toggleProvider(true)}};
  }
  if(!view.credential)return {key:'need-ai-on',text:'已启用，但 AI 服务缺少可用的密钥和模型。',button:{label:'去配置 AI 服务',icon:'arrowRight',quiet:true,onClick:spotlightAI}};
  if(!view.effective)return {key:`blocked:${view.provider.reason||''}`,text:semanticReasonLabel(view.provider.reason)||'语义分析暂不可用。'};
  if(view.active){
    const pause=caps.pause||{};
    return {key:`run:${Boolean(pause.enabled)}:${view.total>0}`,run:true,text:'在后台进行，可以离开本页；已完成的部分随时可用。',
      button:pause.enabled?{label:'暂停',icon:'pause',quiet:true,action:'pause'}:null};
  }
  if(view.runStatus==='paused'){
    const resume=caps.resume||{};
    return {key:`paused:${Boolean(resume.enabled)}`,text:resume.enabled?`已暂停：完成 ${fmt(view.done)} / ${fmt(view.total)} 道，已完成的部分保留。`:semanticReasonLabel(resume.reason),
      button:{label:'继续',icon:'play',action:'resume',disabled:!resume.enabled}};
  }
  if(LOCKED_RUNS.has(view.runStatus))return {key:`locked:${view.runStatus}`,text:`${semanticRunLabel(view.runStatus)}：需要先完成人工复核，才能继续或重新更新。`};
  const refresh=caps.refresh||{};
  const blocked=refresh.enabled?'':semanticReasonLabel(refresh.reason);
  if(view.snapshot==='missing'&&!view.pending){
    return {key:`first:${Boolean(refresh.enabled)}`,text:blocked||'首次会分析全部题目，会产生 AI 用量，可随时暂停。',
      button:{label:'开始建立索引',icon:'play',action:'refresh',disabled:!refresh.enabled}};
  }
  if(view.pending){
    const estimate=estimateTokens(view.pending);
    return {key:`update:${view.pending}:${Boolean(refresh.enabled)}`,text:blocked||`只处理有变化的题${estimate?`，按上次用量估计约 ${estimate} Token`:''}，可随时暂停。`,
      button:{label:`立即更新 ${fmt(view.pending)} 道`,icon:'refresh',action:'refresh',disabled:!refresh.enabled}};
  }
  return {key:`fresh:${Boolean(refresh.enabled)}:${view.autoEffective}`,
    text:view.autoEffective?'已是最新。题库重建后会自动接着更新。':'已是最新。题库重建过的话，点“检查更新”补上变化的题。',
    button:{label:'检查更新',icon:'refresh',action:'refresh',quiet:true,text:true,disabled:!refresh.enabled}};
}

/* 提交中的按钮保持可聚焦（aria-disabled），焦点不会因为重绘丢到页面顶部 */
function primaryButton(spec){
  const className=spec.text?'set-text-btn':`btn btn-sm ${spec.quiet?'btn-out':'btn-p'}`;
  const busy=Boolean(actionPending)&&spec.action===actionPending;
  return h('button',{type:'button',class:className,disabled:spec.disabled||(actionPending&&!busy)?true:null,
    'aria-busy':busy?'true':null,'aria-disabled':busy?'true':null,
    onclick:()=>{
      if(actionPending)return;
      if(spec.action)void runSemanticAction(spec.action);
      else spec.onClick?.();
    }},busy?ui.iconNode('loader','icon-spin'):ui.iconNode(spec.icon),busy?'提交中…':spec.label);
}

function runProgress(view){
  const ratio=view.total?Math.min(1,view.done/view.total):null;
  return [ratio,view.total?`${fmt(view.done)} / ${fmt(view.total)} 道`:'正在准备…'];
}

/* 主按钮：状态没变时只更新进度，不重建按钮，免得抢走焦点 */
function renderPrimary(view){
  const slot=el('sem-primary'),hint=el('sem-primary-hint');
  if(!slot)return;
  const plan=primaryPlan(view);
  const key=`${plan.key}|${actionPending}`;
  if(key!==primaryKey){
    const hadFocus=Boolean(slot.contains?.(document.activeElement));
    const parts=[];
    if(plan.run){
      const [ratio,label]=runProgress(view);
      parts.push(h('span',{class:'sem-run'},
        h('span',{class:`set-progress${ratio==null?' is-indeterminate':''}`},h('span',{style:ratio==null?null:`width:${Math.max(4,ratio*100)}%`})),
        h('small',{text:label})));
    }
    if(plan.button)parts.push(primaryButton(plan.button));
    slot.replaceChildren(...parts);
    primaryKey=key;
    if(hadFocus)(slot.querySelector('button:not(:disabled)')||el('semantic-index-refresh'))?.focus({preventScroll:true});
  }else if(plan.run){
    const [ratio,label]=runProgress(view);
    const bar=slot.querySelector?.('.sem-run .set-progress>span');
    if(bar&&ratio!=null)bar.style.width=`${Math.max(4,ratio*100)}%`;
    const text=slot.querySelector?.('.sem-run small');
    if(text)text.textContent=label;
  }
  if(hint)hint.textContent=plan.text;
}

function renderSemanticMessage(view){
  const message=actionMessage||(view?.runStatus==='failed'&&overview?.last_error?.message
    ?{text:`最近一次更新失败：${overview.last_error.message}`,tone:'error'}:null)
    ||(!view&&overviewError?{text:`读取失败：${overviewError}`,tone:'error'}:null);
  ui.setMessage(el('semantic-index-message'),message?.text||'',message?.tone||'');
}

function renderSemanticControls(view){
  const ready=Boolean(view);
  const settings=view?.settings||{};
  const mode=['off','shadow','enforced'].includes(settings.recall_mode)?settings.recall_mode:'shadow';
  const modeSeg=el('semantic-recall-mode');
  ui.setSeg(modeSeg,mode);
  modeSeg?.querySelectorAll?.('[role="radio"]').forEach(button=>{button.disabled=!ready});
  const modeHint=el('sem-mode-hint');
  if(modeHint)modeHint.textContent=!ready?''
    :mode==='enforced'&&view.state.reasons?.recall_mode?semanticReasonLabel(view.state.reasons.recall_mode):MODE_HINTS[mode](view);

  const providerSwitch=el('semantic-provider-enabled');
  ui.setSwitch(providerSwitch,view?.requested,{disabled:!ready||view.managed||(!view.credential&&!view.requested),busy:savingControls.has('semantic-provider-enabled')});
  const providerHint=el('semantic-provider-hint');
  if(providerHint)providerHint.textContent=!ready?''
    :view.managed?`由${view.provider.source==='environment'?'环境变量':'启动配置'}管理，这里不能修改`
    :!view.credential?(view.requested?'已启用，但 AI 服务缺少可用密钥':'先在 AI 服务里保存可用的密钥和模型')
    :!view.requested?'关闭时不会调用 AI'
    :view.effective?'已启用 · 更新时会产生 AI 用量':semanticReasonLabel(view.provider.reason);

  const autoOn=Boolean(settings.auto_refresh_enabled);
  ui.setSwitch(el('semantic-auto-refresh'),autoOn,{disabled:!ready||!view.requested,busy:savingControls.has('semantic-auto-refresh')});
  const autoHint=el('semantic-auto-refresh-hint');
  if(autoHint)autoHint.textContent=!ready?''
    :!view.requested?'需先启用语义分析'
    :!autoOn?'题库更新后需要手动点“立即更新”'
    :view.autoEffective?'普通题库重建后自动接着更新':semanticReasonLabel(view.state.reasons?.auto_refresh_enabled);

  const concurrency=String(settings.max_concurrency||1);
  const concurrencySeg=el('semantic-max-concurrency');
  ui.setSeg(concurrencySeg,concurrency);
  concurrencySeg?.querySelectorAll?.('[role="radio"]').forEach(button=>{button.disabled=!ready});
  const multiplier=Math.min(10,Math.max(1,num(settings.candidate_multiplier)||4));
  const output=el('semantic-candidate-multiplier');
  if(output)output.textContent=String(multiplier);
  el('semantic-multiplier-stepper')?.querySelectorAll?.('[data-step]').forEach(button=>{
    button.disabled=!ready||(button.dataset.step==='-1'?multiplier<=1:multiplier>=10);
  });
  const partialOn=Boolean(settings.allow_partial_cache);
  ui.setSwitch(el('semantic-allow-partial'),partialOn,{disabled:!ready,busy:savingControls.has('semantic-allow-partial')});
  const partialHint=el('semantic-partial-hint');
  if(partialHint)partialHint.textContent=ready&&partialOn&&!view.state.effective?.allow_partial_cache&&view.state.reasons?.allow_partial_cache
    ?semanticReasonLabel(view.state.reasons.allow_partial_cache):'能更早用上语义辅助，但可能漏掉一些题';
  const advanced=el('sem-adv-value');
  if(advanced)advanced.textContent=ready?`并发 ${concurrency} · 候选池 ×${multiplier} · ${partialOn?'先用部分结果':'等全部完成'}`:'';
}

function renderSemanticDetails(view){
  const node=el('semantic-index-details');
  if(!node)return;
  if(!view){node.replaceChildren();return}
  const run=view.run,tokens=overview.tokens||{},projection=overview.projection||{},stop=overview.stop||{};
  definitions(node,[
    ['最近一次运行',run?`${semanticRunLabel(view.runStatus)} · ${timeLabel(run.finished_at||run.updated_at||run.created_at,'—')}`:'无'],
    run?['本次运行',`共 ${fmt(view.total)} 道 · 完成 ${fmt(run.completed_count)} · 失败 ${fmt(run.failed_count)}`]:null,
    run?['本次用量',`${settingsRender.wanTokens(tokens.consumed)} Token${num(tokens.unverified)?`（未核验 ${fmt(tokens.unverified)}）`:''}`]:null,
    ['最近发布',timeLabel(overview.semantic_snapshot?.published_at,'尚无')],
    ['检索投影',projection.id?`${projection.schema||'未知算法'} · 权重 ${projection.version||'未知'}`:'无'],
    ['实际并发',`${num(overview.concurrency?.effective)||'—'}（设置为 ${String(view.settings.max_concurrency||1)}）`],
    stop.reason?['暂停原因',semanticReasonLabel(stop.reason)]:null,
    overview.last_error?.message?['最近错误',String(overview.last_error.message)]:null
  ]);
}

function renderSemantic(){
  const view=semanticView();
  ui.setState(el('semantic-index-state'),semanticStatus());
  renderCoverage(view);
  renderPrimary(view);
  renderSemanticMessage(view);
  renderSemanticControls(view);
  renderSemanticDetails(view);
}

/* ── ⑥ 语义索引：读取与轮询 ── */
function scheduleSemanticPoll(){
  ui.cancel(semanticPollTimer);semanticPollTimer=0;
  if(settingsState.active&&!document.hidden&&overview?.job_state?.semantic_active){
    semanticPollTimer=ui.later(()=>{semanticPollTimer=0;void loadSemanticIndexStatus({silent:true})},1500);
  }
}

/* 同时只发一个读取；fresh 用于保存之后，必须拿到保存后的状态 */
function loadSemanticIndexStatus(options={}){
  if(statusRequest&&!options.fresh)return statusRequest;
  const operation=settingsAPI.beginOperation('loadSemanticIndexStatus');
  const button=el('semantic-index-refresh');
  if(!options.silent)ui.spinning(button,true);
  const request=(async()=>{
    try{
      const payload=await settingsAPI.request(settingsAPI.paths.semanticOverview);
      if(!operation.current())return null;
      if(payload?.schema!=='semantic-index-operations-v1')throw new Error('状态契约版本不兼容');
      overview=overlay(payload);
      overviewError='';
      return payload;
    }catch(error){
      if(!operation.current()||error?.name==='AbortError')return null;
      overviewError=error.message||'未知错误';
      if(overview&&!options.silent)actionMessage={text:`读取失败：${overviewError}`,tone:'error'};
      return null;
    }finally{
      if(operation.current()){
        statusRequest=null;
        ui.spinning(button,false);
        renderSemantic();renderBank();scheduleSemanticPoll();ui.changed();
      }
    }
  })();
  statusRequest=request;
  return request;
}

const semanticErrorText=error=>{
  const message=String(error?.data?.message||error?.message||'未知错误');
  return /^[a-z][a-z_]+$/.test(message)?semanticReasonLabel(message):message;
};
const isConflict=error=>error?.data?.code==='semantic_settings_conflict';

/* ── ⑥ 语义索引：即改即存（串行；冲突时读回最新版本重试一次） ── */
let saveChain=Promise.resolve();
function settle(patch,activation){
  for(const [field,value] of Object.entries(patch))if(pendingSettings.get(field)===value)pendingSettings.delete(field);
  if(activation&&'provider' in activation&&pendingProvider===activation.provider)pendingProvider=null;
}
function saveSemantic({patch={},activation=null,control}){
  const revision=settingsState.revision,controlId=control?.id||'';
  for(const [field,value] of Object.entries(patch))pendingSettings.set(field,value);
  if(activation&&'provider' in activation)pendingProvider=activation.provider;
  if(controlId)savingControls.add(controlId);
  overlay(overview);
  renderSemantic();renderBank();ui.changed();
  const job=async()=>{
    // 离开页面后排队的保存不再发出，回来时以读回的状态为准
    if(revision!==settingsState.revision||!settingsState.active)return;
    const operation=settingsAPI.beginOperation('saveSemanticSettings');
    const send=()=>settingsAPI.write(settingsAPI.paths.semanticIndex,{
      version:overview?.settings_revision,settings:{...(overview?.settings||{}),...patch},...(activation?{activation}:{})
    },'PUT');
    let saved=false;
    try{
      if(!overview)throw new Error('语义索引状态还没读到');
      try{await send()}
      catch(error){
        if(!isConflict(error))throw error;
        await loadSemanticIndexStatus({silent:true,fresh:true});
        if(!operation.current())return;
        await send();
      }
      if(!operation.current())return;
      saved=true;
      actionMessage=null;
    }catch(error){
      if(!operation.current()||error?.name==='AbortError')return;
      actionMessage={text:`设置没有保存：${semanticErrorText(error)}`,tone:'error'};
    }finally{
      if(operation.current()){
        settle(patch,activation);
        if(controlId)savingControls.delete(controlId);
        await loadSemanticIndexStatus({silent:true,fresh:true});
        if(saved&&settingsState.revision===revision)ui.savedTick(control);
      }
    }
  };
  saveChain=saveChain.then(job,job);
  return saveChain;
}

async function toggleProvider(next){
  if(next&&!await ui.confirm({title:'启用语义分析',type:'info',confirmLabel:'启用',body:[
    '启用后会用当前 AI 服务为题目生成材料卡，更新时会产生 AI 用量。',
    '打开本页或查看状态不会启动任务：需要你点“立即更新”，或打开“题库更新后自动更新”。']}))return;
  await saveSemantic({activation:{provider:next},control:el('semantic-provider-enabled')});
}
async function toggleAuto(next){
  if(next&&!await ui.confirm({title:'题库更新后自动更新',type:'info',confirmLabel:'打开',body:[
    '普通题库索引重建完成后，会自动接着更新语义索引，并产生 AI 用量。','题库没有变化时不会重复提交。']}))return;
  await saveSemantic({patch:{auto_refresh_enabled:next},activation:next?{auto_refresh:true}:null,control:el('semantic-auto-refresh')});
}
async function togglePartial(next){
  if(next&&!await ui.confirm({title:'先用已完成部分',type:'warning',confirmLabel:'打开',body:[
    '索引还没更新完时，组卷先用已经完成的材料卡。','能更早用上语义辅助，但没有材料卡的题可能被漏掉。']}))return;
  await saveSemantic({patch:{allow_partial_cache:next},activation:next?{allow_partial:true}:null,control:el('semantic-allow-partial')});
}

/* 候选池倍数：连点时只在停下 0.5 秒后保存一次 */
let multiplierTimer=0;
function stepMultiplier(step){
  if(!overview)return;
  const current=Math.min(10,Math.max(1,num(overview.settings?.candidate_multiplier)||4));
  const next=Math.min(10,Math.max(1,current+step));
  if(next===current)return;
  pendingSettings.set('candidate_multiplier',next);
  overlay(overview);
  renderSemanticControls(semanticView());
  ui.cancel(multiplierTimer);
  multiplierTimer=ui.later(()=>{
    multiplierTimer=0;
    void saveSemantic({patch:{candidate_multiplier:pendingSettings.get('candidate_multiplier')??next},control:el('semantic-multiplier-stepper')});
  },500);
}

/* ── ⑥ 语义索引：运维操作 ── */
const ACTION_LABELS=Object.freeze({refresh:'更新',pause:'暂停',resume:'继续',rebuild:'完整重建'});
async function runSemanticAction(action,{confirm=false}={}){
  if(!overview||actionPending)return;
  const operation=settingsAPI.beginOperation('runSemanticAction');
  actionPending=action;
  renderPrimary(semanticView());
  const send=()=>settingsAPI.write(settingsAPI.paths.semanticAction(action),{
    version:overview?.settings_revision,run_id:overview?.latest_run?.id||null,confirm});
  try{
    try{await send()}
    catch(error){
      if(!isConflict(error))throw error;
      await loadSemanticIndexStatus({silent:true,fresh:true});
      if(!operation.current())return;
      await send();
    }
    if(!operation.current())return;
    actionMessage=null;
    window.refreshJobs?.();
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    actionMessage={text:`${ACTION_LABELS[action]||'操作'}没有成功：${semanticErrorText(error)}`,tone:'error'};
  }finally{
    if(operation.current()){
      actionPending='';
      await loadSemanticIndexStatus({silent:true,fresh:true});
    }
  }
}

async function confirmRebuild(){
  const view=semanticView();
  if(!view)return;
  const estimate=estimateTokens(view.eligible);
  const ok=await ui.confirm({title:'完整重建语义索引',type:'warning',confirmLabel:'开始完整重建',body:[
    `将从头分析全部 ${fmt(view.eligible)} 道题${estimate?`，按上次用量估计约 ${estimate} Token`:''}，耗时较长。`,
    '旧结果会保留；新结果全部完成并检查通过后才替换。平时用“立即更新”就够了。']});
  if(ok)await runSemanticAction('rebuild',{confirm:true});
}

async function previewCleanup(){
  const operation=settingsAPI.beginOperation('previewSemanticCleanup');
  try{
    const preview=await settingsAPI.request(settingsAPI.paths.semanticCleanupPreview);
    if(!operation.current())return;
    const counts=preview?.counts||{};
    const parts=[['旧材料卡',counts.superseded_cards,'张'],['部分快照',counts.partial_snapshots,'个'],['旧检索投影',counts.old_projections,'个'],
      ['已结束的运行记录',counts.terminal_runs,'条'],['私有材料缓存',counts.private_material_cache,'项']].filter(([,value])=>num(value)>0);
    await ui.notice({title:'可清理的历史',type:'info',body:[
      parts.length?`可清理：${parts.map(([label,value,unit])=>`${label} ${fmt(value)} ${unit}`).join('、')}。`:'没有可清理的历史。',
      '这只是预览，没有删除任何内容。']});
  }catch(error){
    if(!operation.current()||error?.name==='AbortError')return;
    actionMessage={text:`清理预览没有读到：${semanticErrorText(error)}`,tone:'error'};
    renderSemanticMessage(semanticView());
  }
}

function openSemanticMenu(trigger){
  const rebuild=overview?.capabilities?.rebuild||{};
  ui.toggleMenu(trigger,node=>node.append(
    ui.menuItem({icon:'rotateCcw',label:'完整重建…',disabled:!rebuild.enabled||Boolean(actionPending),
      hint:rebuild.enabled?'从头分析全部题目，最耗时、最费用量':(semanticReasonLabel(rebuild.reason)||'当前不可用'),onClick:confirmRebuild}),
    ui.menuItem({icon:'archive',label:'清理历史…',hint:'先预览可清理的旧版本，不会直接删除',disabled:!overview,onClick:previewCleanup})
  ),{label:'语义索引：更多操作'});
}

/* ── 绑定 ── */
el('question-bank-refresh')?.addEventListener('click',()=>loadQuestionBankStatus());
el('question-bank-rebuild')?.addEventListener('click',()=>rebuildQuestionBank());
el('semantic-index-refresh')?.addEventListener('click',()=>{actionMessage=null;void loadSemanticIndexStatus()});
el('sem-more')?.addEventListener('click',event=>openSemanticMenu(event.currentTarget));
ui.bindSeg(el('semantic-recall-mode'),value=>void saveSemantic({patch:{recall_mode:value},control:el('semantic-recall-mode')}));
ui.bindSeg(el('semantic-max-concurrency'),value=>void saveSemantic({patch:{max_concurrency:Number(value)||1},control:el('semantic-max-concurrency')}));
ui.bindSwitch(el('semantic-provider-enabled'),toggleProvider);
ui.bindSwitch(el('semantic-auto-refresh'),toggleAuto);
ui.bindSwitch(el('semantic-allow-partial'),togglePartial);
el('semantic-multiplier-stepper')?.addEventListener('click',event=>{
  const button=event.target.closest?.('[data-step]');
  if(button&&!button.disabled)stepMultiplier(Number(button.dataset.step)||0);
});

window.addEventListener('cuoti:job-update',event=>{
  const job=event.detail;
  if(job?.kind==='semantic-index-refresh'){
    if(settingsState.active)void loadSemanticIndexStatus({silent:true});
    return;
  }
  if(job?.kind!=='question-bank-rebuild')return;
  if(TERMINAL_JOBS.has(job.status)){
    rebuildKey=null;
    bankJob=null;
    bankNote=job.status==='failed'?{text:`重建失败：${jobError(job)||'请到后台任务里查看原因'}`,tone:'error'}
      :job.status==='cancelled'?{text:'重建已取消，旧索引照常可用。',tone:''}
      :{text:`重建完成，组卷已在使用新索引。${relayText(job.result?.auto_refresh)}`,tone:'ok'};
    if(settingsState.active){
      void loadQuestionBankStatus({silent:true});
      void loadSemanticIndexStatus({silent:true});
    }
    return;
  }
  bankJob=job;
  if(settingsState.active&&bank){bank.rebuilding=true;renderBank();ui.changed()}
});

document.addEventListener('visibilitychange',()=>{
  if(!settingsState.active)return;
  if(document.hidden){ui.cancel(semanticPollTimer);ui.cancel(bankPollTimer);semanticPollTimer=0;bankPollTimer=0;return}
  void loadSemanticIndexStatus({silent:true});
  void loadQuestionBankStatus({silent:true});
});

window.loadSemanticIndexStatus=loadSemanticIndexStatus;
window.runSemanticAction=runSemanticAction;
window.loadQuestionBankStatus=loadQuestionBankStatus;

function unmount(){
  ui.cancel(semanticPollTimer);ui.cancel(bankPollTimer);ui.cancel(multiplierTimer);
  semanticPollTimer=0;bankPollTimer=0;multiplierTimer=0;
  statusRequest=null;primaryKey='';actionPending='';
  pendingSettings.clear();pendingProvider=null;savingControls.clear();
}

settingsPage.maintenance=Object.freeze({loadSemanticIndexStatus,loadQuestionBankStatus,runSemanticAction,unmount,
  bankStatus,semanticStatus,focusPrimary:()=>el('sem-primary')?.querySelector('button:not(:disabled)')});
})();
