(function initSettingsRender(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.settings=window.Cuoti.settings||{};
  const el=id=>document.getElementById(id);
  const SEMANTIC_MODE_LABELS=Object.freeze({
    off:'关闭语义辅助',shadow:'仅观察比较',enforced:'正式参与排序'
  });
  const SEMANTIC_RUN_LABELS=Object.freeze({
    scanning:'扫描来源',queued:'等待执行',running:'正在更新',retry_wait:'等待重试',
    cancel_requested:'正在取消',paused:'已暂停',paused_budget_review:'因预算暂停',
    paused_token_hard_limit:'因 Token 上限暂停',outcome_unknown:'结果待人工确认',
    succeeded:'已完成',failed:'失败',cancelled:'已取消',complete:'已完成',partial:'部分完成'
  });
  const SEMANTIC_REASON_LABELS=Object.freeze({
    disabled:'尚未启用',provider_disabled:'语义分析尚未启用',
    provider_managed_disabled:'语义分析已被启动配置关闭',
    ai_credential_missing:'已启用但尚不可用：请先在 AI 服务中保存可用密钥和模型',
    activation_required:'已选择，首次启用仍需确认',settings_invalid:'本机设置无效',
    active_job_conflict:'已有语义或组卷任务正在运行',not_implemented:'当前版本尚未提供',
    no_pausable_run:'当前没有可暂停任务',no_resumable_run:'当前没有可继续任务',
    manual_review_required:'需要先完成人工复核',preview_required:'请先查看可清理历史',
    source_kind_unsupported:'当前来源类型尚未接入自动更新',
    ordinary_last_good_unavailable:'普通题库最近可用索引不可用',
    semantic_source_not_configured:'语义来源尚未配置完整',
    home_institution_mismatch:'正式真题来源与本机院校设置不一致',
    snapshot_already_indexed:'该来源快照已经完成索引',
    database_unavailable:'数据库暂时不可用',idempotency_conflict:'任务幂等状态冲突',
    analysis_profile_unavailable:'语义分析模型配置不可用'
  });
  const AI_PROFILE_TYPE_LABELS=Object.freeze({
    explicit_preference:'明确偏好',inferred_weakness:'薄弱点',
    mastery_signal:'掌握信号',model_observation:'模型观察'
  });
  const AI_PROFILE_STATUS_LABELS=Object.freeze({
    recent:'近期',stable:'稳定',confirmed:'已确认',rejected:'已否定',suppressed:'已隐藏'
  });
  const AI_PROFILE_ACTIONS=Object.freeze([
    Object.freeze({action:'confirm',label:'确认'}),
    Object.freeze({action:'correct',label:'纠正'}),
    Object.freeze({action:'reject',label:'否定'}),
    Object.freeze({action:'suppress',label:'隐藏'})
  ]);
  const semanticReasonLabel=value=>{
    const code=String(value||'').trim();
    if(!code)return '';
    return SEMANTIC_REASON_LABELS[code]||`未知状态（${code}）`;
  };
  const semanticRunLabel=value=>{
    const code=String(value||'').trim();
    if(!code)return '无';
    return SEMANTIC_RUN_LABELS[code]||`未知状态（${code}）`;
  };
  const semanticTimeLabel=value=>{
    if(!value)return '尚无';
    const parsed=new Date(String(value));
    return Number.isNaN(parsed.getTime())?String(value):parsed.toLocaleString('zh-CN');
  };

  const fields=Object.freeze({
    active_profile:'ai-profile',
    base_url:'ai-base-url',
    dns_mode:'ai-dns-mode',
    dns_servers:'ai-dns-servers',
    quality_model:'ai-quality-model',
    fast_model:'ai-fast-model',
    reasoning_effort:'ai-reasoning-effort',
    classification:'ai-route-classification',
    problem_analysis:'ai-route-analysis',
    daily_analysis:'ai-route-daily',
    exam_generation:'ai-route-exam'
  });

  function normalize(payload){
    const root=payload?.ai||payload||{};
    const selectedId=String(root.default_provider||payload?.active_profile||payload?.provider||'deepseek');
    const providerMap=root.profiles&&typeof root.profiles==='object'
      ?root.profiles
      :(root.providers&&typeof root.providers==='object'?root.providers:{});
    const provider=providerMap[selectedId]||payload?.provider_config||root;
    const models=provider.models||{};
    const assignments=root.assignments||payload?.assignments||{};
    const dnsServers=provider.dns_servers??payload?.dns_servers??['223.5.5.5','119.29.29.29'];
    return {
      id:selectedId,
      name:String(provider.name||selectedId),
      connectionType:String(provider.connection_type||'openai_compatible'),
      profiles:providerMap,
      adapter:String(provider.adapter||provider.provider||payload?.provider||'openai_compatible'),
      baseURL:String(provider.base_url||provider.api_url||payload?.base_url||payload?.deepseek_url||''),
      dnsMode:String(provider.dns_mode||payload?.dns_mode||'domestic'),
      dnsServers:Array.isArray(dnsServers)?dnsServers.join(', '):String(dnsServers||''),
      qualityModel:String(models.quality||provider.quality_model||payload?.quality_model||payload?.deepseek_model||''),
      fastModel:String(models.fast||provider.fast_model||payload?.fast_model||payload?.deepseek_flash_model||''),
      reasoningEffort:String(provider.reasoning_effort??payload?.reasoning_effort??''),
      keyConfigured:Boolean(provider.api_key_configured??provider.has_api_key??payload?.api_key_configured??payload?.has_api_key??payload?.configured),
      source:String(provider.credential_source||provider.source||payload?.credential_source||payload?.source||'local'),
      lockedFields:Array.isArray(provider.managed_fields)?provider.managed_fields:(Array.isArray(payload?.managed_fields)?payload.managed_fields:(Array.isArray(payload?.locked_fields)?payload.locked_fields:[])),
      assignments,
      version:payload?.version??payload?.etag??null
    };
  }

  function snapshot(settings){
    const assignmentValue=(name,fallback)=>settings.assignments?.[name]?.model_role||settings.assignments?.[name]||fallback;
    return {
      active_profile:settings.id,
      base_url:settings.baseURL,
      dns_mode:settings.dnsMode,
      dns_servers:settings.dnsServers,
      quality_model:settings.qualityModel,
      fast_model:settings.fastModel,
      reasoning_effort:settings.reasoningEffort,
      classification:assignmentValue('classification','fast'),
      problem_analysis:assignmentValue('problem_analysis','quality'),
      daily_analysis:assignmentValue('daily_analysis','quality'),
      exam_generation:assignmentValue('exam_generation','quality')
    };
  }

  function aiProfileList(items){
    const root=el('ai-memory-profile-list');
    if(!root)return 0;
    root.replaceChildren();
    const entries=Array.isArray(items)?items:[];
    entries.forEach(item=>{
      if(!item||typeof item!=='object')return;
      const key=String(item.key??'').trim();
      if(!key)return;
      const hasCorrection=typeof item.correction==='string'&&item.correction.length>0;
      const rawValue=hasCorrection?item.effective_value:(item.effective_value??item.value);
      const value=String(rawValue??'').trim()||'（空值）';
      const entryType=String(item.entry_type??'');
      const status=String(item.status??'');
      const rawConfidence=Number(item.confidence);
      const confidence=Number.isFinite(rawConfidence)
        ?Math.round(Math.max(0,Math.min(1,rawConfidence))*100)
        :0;

      const article=document.createElement('article');
      article.className='health-check ai-profile-entry';
      article.dataset.key=key;
      const heading=document.createElement('strong');
      heading.textContent=`${AI_PROFILE_TYPE_LABELS[entryType]||'画像'} · ${value}`;
      const details=document.createElement('span');
      details.textContent=`${AI_PROFILE_STATUS_LABELS[status]||'状态未知'} · 证据 ${Math.max(0,safeInteger(item.evidence_count))} 条 · 置信度 ${confidence}%`;
      if(hasCorrection){
        const corrected=document.createElement('span');
        corrected.className='ai-profile-corrected';
        corrected.textContent='已采用用户纠正';
        details.appendChild(corrected);
      }
      const actions=document.createElement('div');
      actions.className='settings-actions';
      AI_PROFILE_ACTIONS.forEach(command=>{
        const button=document.createElement('button');
        button.type='button';
        button.className=`btn ${command.action==='reject'?'btn-d':'btn-out'} btn-xs`;
        button.dataset.aiProfileAction=command.action;
        button.textContent=command.label;
        button.setAttribute('aria-label',`${command.label}此条画像`);
        const isCurrent=(command.action==='confirm'&&status==='confirmed')||
          (command.action==='reject'&&status==='rejected')||
          (command.action==='suppress'&&status==='suppressed');
        button.dataset.profileCurrent=String(isCurrent);
        button.disabled=isCurrent;
        actions.appendChild(button);
      });
      article.append(heading,details,actions);
      root.appendChild(article);
    });
    if(!root.childElementCount){
      const empty=document.createElement('div');
      empty.className='settings-message';
      empty.textContent='当前筛选下没有画像条目';
      root.appendChild(empty);
    }
    return root.querySelectorAll('.ai-profile-entry').length;
  }

  function questionBankStatus(payload){
    const state=el('question-bank-state');
    const details=el('question-bank-details');
    const message=el('question-bank-message');
    if(!state||!details||!message)return Boolean(payload?.rebuilding);
    const label=payload?.rebuilding?'正在重建':(!payload?.available?'尚无可用索引':(payload?.stale?'旧索引可用，等待重建':'索引最新'));
    state.textContent=label;
    state.className=`settings-state ${!payload?.available?'health-error':(payload?.stale?'health-warning':'health-ok')}`;
    details.replaceChildren();
    const coverage=payload?.coverage||{};
    const rows=[
      ['最近可用快照',payload?.last_good_snapshot?String(payload.last_good_snapshot).slice(0,16)+'…':'无'],
      ['题源覆盖',`${payload?.sources?.available||0}/${payload?.sources?.configured||0} 个文件`],
      ['有效题目',`${coverage?.total_questions||0} 道`],
      ['最近成功',payload?.last_success_at||'尚无'],
    ];
    rows.forEach(([name,value])=>{
      const item=document.createElement('div'); item.className='health-check';
      const strong=document.createElement('strong'); strong.textContent=name;
      const span=document.createElement('span'); span.textContent=value;
      item.append(strong,span); details.appendChild(item);
    });
    const automatic=payload?.latest_job?.result?.auto_refresh;
    const automaticText=automatic
      ?automatic.status==='submitted'?'语义自动更新：已提交新任务'
      :automatic.status==='replayed'?'语义自动更新：已复用相同来源任务'
      :automatic.status==='skipped'?`语义自动更新未执行：${semanticReasonLabel(automatic.reason)}`
      :automatic.status==='deferred'?`语义自动更新暂缓：${semanticReasonLabel(automatic.reason)}`
      :`语义自动更新：未知状态（${String(automatic.status||'unknown')}）`
      :'';
    message.textContent=[payload?.error?`最近失败：${payload.error}`:'',automaticText].filter(Boolean).join('；');
    message.className=`settings-message${payload?.error||automatic?.status==='deferred'?' error':''}`;
    const job=payload?.job||payload?.latest_job;
    if(job?.progress?.message&&payload?.rebuilding){
      message.textContent=job.progress.message;
      message.className='settings-message';
    }
    return Boolean(payload?.rebuilding);
  }

  function runtimeHealth(payload){
    const state=el('runtime-health-state');
    const list=el('runtime-health-checks');
    const status=el('runtime-health-status');
    if(!state||!list||!status)return;
    const labels={healthy:'运行正常',degraded:'部分功能受限',fatal:'核心服务不可用'};
    const checkLabels={database:'主数据库',job_worker:'后台任务',xelatex:'PDF 编译',question_bank:'普通题库',ai_configuration:'AI 配置'};
    const checkStatuses={ok:'正常',degraded:'警告',fatal:'失败'};
    const tones={healthy:'health-ok',degraded:'health-warning',fatal:'health-error'};
    state.textContent=labels[payload?.overall_status]||'状态未知';
    state.className=`settings-state ${tones[payload?.overall_status]||''}`.trim();
    list.replaceChildren();
    (Array.isArray(payload?.checks)?payload.checks:[]).forEach(check=>{
      const item=document.createElement('div');
      item.className=`health-check health-${String(check?.status||'degraded')}`;
      const name=document.createElement('strong');
      const checkId=String(check?.check_id||'unknown');
      const checkStatus=String(check?.status||'unknown');
      name.textContent=`${checkLabels[checkId]||`未知检查（${checkId}）`} · ${checkStatuses[checkStatus]||`未知状态（${checkStatus}）`}`;
      const message=document.createElement('span');
      message.textContent=String(check?.message||'未提供诊断信息');
      item.append(name,message);
      list.appendChild(item);
    });
    const checked=new Date(String(payload?.checked_at||''));
    status.textContent=Number.isNaN(checked.getTime())?'诊断已完成':`检查时间：${checked.toLocaleTimeString('zh-CN')}`;
  }

  function semanticIndexStatus(payload){
    const state=el('semantic-index-state');
    const details=el('semantic-index-details');
    const message=el('semantic-index-message');
    if(!state||!details||!message)return payload;
    const snapshot=payload?.semantic_snapshot||{};
    const coverage=payload?.coverage||{};
    const counts=payload?.counts||{};
    const tokens=payload?.tokens||{};
    const stop=payload?.stop||{};
    const labels={complete:'完整快照',partial:'部分快照',missing:'无快照'};
    const mode=String(payload?.settings?.recall_mode||'shadow');
    state.textContent=`${SEMANTIC_MODE_LABELS[mode]||`未知模式（${mode}）`} · ${labels[snapshot.status]||`未知快照（${snapshot.status||'unknown'}）`}`;
    state.className=`settings-state ${snapshot.status==='complete'?'health-ok':(snapshot.status==='partial'?'health-warning':'health-error')}`;
    const percent=Math.max(0,Math.min(100,Number(coverage.ratio||0)*100)).toFixed(1);
    const rows=[
      ['覆盖率',`${percent}% · ${counts.valid||0}/${counts.eligible||0}`],
      ['卡片状态',`过期 ${counts.stale||0} · 缺失 ${counts.missing||0} · 排除 ${counts.excluded||0}`],
      ['最近任务',payload?.latest_run?`${payload.latest_run.id} · ${semanticRunLabel(payload.latest_run.status)}`:'无'],
      ['任务计数',`完成 ${counts.completed||0} · 失败 ${counts.failed||0} · 待重试 ${counts.retry_pending||0} · 弃用 ${counts.abandoned||0}`],
      ['投影版本',payload?.projection?.id?`${payload.projection.schema||'unknown'} · ${payload.projection.version||'unknown'}`:'无'],
      ['Token',`已用 ${tokens.consumed||0} · 预留 ${tokens.reserved||0} · 未核验 ${tokens.unverified||0}`],
      ['并发',`${payload?.concurrency?.effective||0}/${payload?.concurrency?.configured||1}`],
      ['最近更新',semanticTimeLabel(snapshot.published_at)],
    ];
    details.replaceChildren();
    rows.forEach(([name,value])=>{
      const item=document.createElement('div');item.className='health-check';
      const strong=document.createElement('strong');strong.textContent=name;
      const span=document.createElement('span');span.textContent=String(value);
      item.append(strong,span);details.appendChild(item);
    });
    const errors=[];
    if(stop.reason)errors.push(`暂停或降级：${semanticReasonLabel(stop.reason)}`);
    if(payload?.last_error?.message)errors.push(`最近错误：${payload.last_error.message}`);
    message.textContent=errors.join('；');
    message.className=`settings-message${errors.length?' error':''}`;
    el('semantic-recall-mode').value=payload?.settings?.recall_mode||'shadow';
    el('semantic-max-concurrency').value=String(payload?.settings?.max_concurrency||1);
    el('semantic-candidate-multiplier').value=String(payload?.settings?.candidate_multiplier||4);
    el('semantic-auto-refresh').checked=Boolean(payload?.settings_state?.effective?.auto_refresh_enabled);
    el('semantic-allow-partial').checked=Boolean(payload?.settings_state?.effective?.allow_partial_cache);
    const provider=payload?.settings_state?.provider||{};
    const reasons=payload?.settings_state?.reasons||{};
    const providerInput=el('semantic-provider-enabled');
    providerInput.checked=Boolean(provider.requested);
    providerInput.disabled=Boolean(provider.managed)||(!provider.credential_configured&&!provider.requested);
    providerInput.setAttribute('aria-disabled',String(providerInput.disabled));
    const providerHint=el('semantic-provider-hint');
    providerHint.textContent=provider.requested&&!provider.credential_configured
      ?`已启用但尚不可用：请先在上方 AI 服务中保存可用密钥和模型${provider.managed?'；该开关由环境或启动配置管理':''}。`
      :provider.managed
      ?`由${provider.source==='environment'?'环境变量':'启动配置'}管理，页面不能修改。`
      :(!provider.credential_configured?'请先在上方 AI 服务中保存可用密钥和模型。':semanticReasonLabel(reasons.provider_enabled));
    const autoHint=el('semantic-auto-refresh-hint');
    autoHint.textContent=Boolean(payload?.settings?.auto_refresh_enabled)&&!Boolean(payload?.settings_state?.effective?.auto_refresh_enabled)
      ?semanticReasonLabel(reasons.auto_refresh_enabled):'';
    const partialHint=el('semantic-partial-hint');
    partialHint.textContent=Boolean(payload?.settings?.allow_partial_cache)&&!Boolean(payload?.settings_state?.effective?.allow_partial_cache)
      ?semanticReasonLabel(reasons.allow_partial_cache):'';
    const activationMessages=[];
    if(reasons.provider_enabled)activationMessages.push(`语义分析：${semanticReasonLabel(reasons.provider_enabled)}`);
    if(reasons.auto_refresh_enabled)activationMessages.push(`自动更新：${semanticReasonLabel(reasons.auto_refresh_enabled)}`);
    if(reasons.allow_partial_cache)activationMessages.push(`部分结果：${semanticReasonLabel(reasons.allow_partial_cache)}`);
    if(activationMessages.length)message.textContent=[message.textContent, ...activationMessages].filter(Boolean).join('；');
    ['refresh','pause','resume','rebuild'].forEach(action=>{
      const button=el(`semantic-action-${action}`);
      const capability=payload?.capabilities?.[action];
      if(!button)return;
      button.disabled=!capability?.enabled;
      button.title=capability?.enabled?'':(semanticReasonLabel(capability?.reason)||'当前不可用');
    });
    window.Cuoti.ui.selects?.syncAll(el('p-settings'));
    return payload;
  }

  window.Cuoti.settings.render=Object.freeze({fields,normalize,snapshot,aiProfileList,questionBankStatus,runtimeHealth,semanticIndexStatus,semanticReasonLabel,semanticRunLabel});
})();
