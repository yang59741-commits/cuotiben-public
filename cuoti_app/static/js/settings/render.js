(function initSettingsRender(){
  'use strict';
  /* 设置页的数据整理与中文标签：接口字段到页面文案的映射都集中在这里，卡片模块只负责摆放。 */

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.settings=window.Cuoti.settings||{};

  const SEMANTIC_MODE_LABELS=Object.freeze({
    off:'关闭',shadow:'仅观察比较',enforced:'正式参与排序'
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
    analysis_profile_unavailable:'语义分析模型配置不可用',
    promotion_approval_required:'“正式参与排序”还没通过启用验收，目前按“仅观察比较”运行'
  });
  const AI_PROFILE_TYPE_LABELS=Object.freeze({
    explicit_preference:'明确偏好',inferred_weakness:'薄弱点',
    mastery_signal:'掌握信号',model_observation:'模型观察'
  });
  const AI_PROFILE_STATUS_LABELS=Object.freeze({
    recent:'近期',stable:'稳定',confirmed:'已确认',rejected:'已否定',suppressed:'已隐藏'
  });
  const MEMORY_LABELS=Object.freeze({free:'自由组卷',mistakes:'错题学习',past:'真题组卷'});
  /* 使用情况：按 AI 调用统计时用任务策略名，按任务结果统计时用后台任务类型 */
  const POLICY_LABELS=Object.freeze({
    generic_text:'通用文本',classification_single:'拆题与归类',classification_batch:'批量拆题',
    classification_object:'拆题与归类',classification_batch_object:'批量拆题',
    problem_analysis:'错题分析',daily_analysis:'每日分析',exam_generation:'智能组卷',
    structured_exam:'结构化组卷',past_structure:'真题结构分析',candidate_selection:'候选题挑选',
    candidate_selection_rubric:'候选题评分',difficulty_review:'难度复核',exam_difficulty_review:'难度复核',
    exam_quality_review:'试卷质量复核',exam_item_repair:'试题修补',material_analysis:'语义材料卡',
    material_analysis_shard:'语义材料卡（分片）',material_analysis_summary:'语义材料汇总',
    semantic_permanent_card:'语义材料卡',document_ocr:'文档识别',connection_test:'连接测试',
    capability_probe:'能力探测'
  });
  const JOB_LABELS=Object.freeze({
    analysis:'错题分析',classification:'题目录入',ocr:'文档识别','batch-mistakes':'批量标错',
    'exam-generation':'试卷生成','exam-recompile':'试卷重新编译','data-import':'数据导入',
    'mistake-export':'错题 PDF 导出','memory-maintenance':'记忆维护','question-bank-rebuild':'题库索引重建'
  });
  const PROFILE_ORDER=Object.freeze(['deepseek','soulecho','monetwl','claude','custom']);
  const PROFILE_META=Object.freeze({
    deepseek:Object.freeze({short:'DeepSeek',mono:'DS'}),
    soulecho:Object.freeze({short:'SoulEcho',mono:'GPT'}),
    monetwl:Object.freeze({short:'Monetwl',mono:'GPT'}),
    claude:Object.freeze({short:'Claude',mono:'C'}),
    custom:Object.freeze({short:'其他兼容服务',mono:'+'})
  });
  const ROUTING_TASKS=Object.freeze([
    Object.freeze({key:'classification',label:'拆题与归类',icon:'listChecks',rec:'fast'}),
    Object.freeze({key:'problem_analysis',label:'错题分析',icon:'lightbulb',rec:'quality'}),
    Object.freeze({key:'daily_analysis',label:'每日分析',icon:'calendar',rec:'quality'}),
    Object.freeze({key:'exam_generation',label:'智能组卷',icon:'examPaper',rec:'quality'})
  ]);
  const TIER_LABELS=Object.freeze({fast:'快速',balanced:'均衡',quality:'高质量'});
  const DNS_LABELS=Object.freeze({domestic:'国内 DNS 直连',system:'系统 DNS 直连',proxy:'系统代理 / TUN'});
  const DNS_HELP=Object.freeze({
    domestic:'用国内 DNS 解析服务商域名后直连，并检查解析结果是否安全。一般保持默认。',
    system:'用系统 DNS 解析后直连，同样会检查解析结果。',
    proxy:'开着 Clash 等代理软件（TUN / Fake-IP 模式）却连不上时选它，请求交给系统代理处理。'
  });
  const HEALTH_CHECKS=Object.freeze({
    database:Object.freeze({label:'数据库',icon:'database',advice:'大部分功能会受影响。请重启应用；仍然失败时，从备份恢复前先导出现有数据。'}),
    job_worker:Object.freeze({label:'后台任务',icon:'activity',advice:'识别、组卷、建索引等耗时工作会一直排队。请重启应用后点“重新诊断”。'}),
    xelatex:Object.freeze({label:'PDF 编译',icon:'fileText',advice:'安装 TeX Live 或 MiKTeX（或配置 xelatex_command）后，点“重新诊断”。'}),
    question_bank:Object.freeze({label:'题库',icon:'files',advice:'确认题库文件夹仍然存在；可用后到“普通题库索引”点“后台重建”。'}),
    ai_configuration:Object.freeze({label:'AI 配置',icon:'sparkles',advice:'到“AI 服务”填写并保存密钥，再点“测试连接”确认。'})
  });
  const HEALTH_STATUS_LABELS=Object.freeze({ok:'正常',degraded:'警告',fatal:'失败'});

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
  const labelOf=(map,key)=>map[key]||`其他（${String(key||'未知')}）`;

  /* 数字与时间 */
  const fmt=value=>Math.round(Number(value)||0).toLocaleString('zh-CN');
  const pct=(value,digits=0)=>`${((Number(value)||0)*100).toFixed(digits)}%`;
  const kTokens=value=>{
    const number=Math.max(0,Number(value)||0);
    return number>=1000?`${(number/1000).toFixed(number>=10000?0:1)}k`:String(Math.round(number));
  };
  const wanTokens=value=>{
    const number=Math.max(0,Number(value)||0);
    return number>=10000?`${(number/10000).toFixed(number>=1000000?0:1)} 万`:fmt(number);
  };
  const pad=value=>String(value).padStart(2,'0');
  function parseTime(value){
    if(!value)return null;
    const parsed=new Date(String(value));
    return Number.isNaN(parsed.getTime())?null:parsed;
  }
  function timeLabel(value,fallback='尚无'){
    const date=parseTime(value);
    if(!date)return value?String(value):fallback;
    const clock=`${pad(date.getHours())}:${pad(date.getMinutes())}`;
    const day=`${pad(date.getMonth()+1)}-${pad(date.getDate())}`;
    return date.getFullYear()===new Date().getFullYear()?`${day} ${clock}`:`${date.getFullYear()}-${day} ${clock}`;
  }
  const clockLabel=(date=new Date())=>`${pad(date.getHours())}:${pad(date.getMinutes())}`;

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
      name:String(provider.name||PROFILE_META[selectedId]?.short||selectedId),
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

  const environmentSource=source=>['env','environment'].includes(String(source||'').toLowerCase());
  /* DNS 服务器统一成英文逗号分隔，接口只按英文逗号拆分 */
  const dnsList=value=>String(value||'').split(/[,，;；\s]+/).map(item=>item.trim()).filter(Boolean);
  const isIPv4=value=>/^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/.test(value);

  window.Cuoti.settings.render=Object.freeze({fields,normalize,snapshot,semanticReasonLabel,semanticRunLabel,labelOf,
    SEMANTIC_MODE_LABELS,AI_PROFILE_TYPE_LABELS,AI_PROFILE_STATUS_LABELS,MEMORY_LABELS,POLICY_LABELS,JOB_LABELS,
    PROFILE_ORDER,PROFILE_META,ROUTING_TASKS,TIER_LABELS,DNS_LABELS,DNS_HELP,HEALTH_CHECKS,HEALTH_STATUS_LABELS,
    fmt,pct,kTokens,wanTokens,timeLabel,clockLabel,parseTime,environmentSource,dnsList,isIPv4});
})();
