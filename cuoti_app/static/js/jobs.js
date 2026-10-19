const ACTIVE_JOB_STATES=new Set([
  'queued','running','cancel_requested','checkpointed','published_pending_receipt'
]);
const JOB_CANCEL_ACTION_STATES=new Set(['queued','running','cancel_requested','checkpointed']);
const TERMINAL_JOB_STATES=new Set(['succeeded','failed','cancelled','interrupted']);
const jobStore=new Map();
const jobWaiters=new Map();
const JOB_TRACKING_KEY='cuoti.jobs.v1';
const JOB_SUBMISSIONS_KEY='cuoti.job-submissions.v1';
const trackedJobIds=new Set();
const jobSubmissionKeys=new Map();
let jobPollTimer=null;
let jobsEndpointAvailable=true;
let jobPollInFlight=false;
let jobInitialHistoryLoaded=false;
const jobCollectionEtags=new Map();

async function fetchJobCollection(url){
  const headers=new Headers();
  const etag=jobCollectionEtags.get(url);
  if(etag)headers.set('If-None-Match',etag);
  let response;
  try{response=await fetch(url,{headers})}
  catch(error){throw new Error('无法连接到本地服务，请确认应用仍在运行。')}
  if(response.status===304)return null;
  const payload=await response.json();
  if(!response.ok){
    const requestError=new Error(payload?.error||`请求失败（HTTP ${response.status}）。`);
    requestError.status=response.status;
    throw requestError;
  }
  const nextEtag=response.headers.get('etag');
  if(nextEtag)jobCollectionEtags.set(url,nextEtag);
  return payload;
}

function loadJobRecoveryState(){
  try{
    const tracked=JSON.parse(localStorage.getItem(JOB_TRACKING_KEY)||'null');
    if(tracked?.schema===1&&Array.isArray(tracked.ids)){
      tracked.ids.slice(0,50).forEach(id=>{
        if(/^[0-9a-f]{32}$/.test(String(id)))trackedJobIds.add(String(id));
      });
    }
  }catch(error){}
  try{
    const submissions=JSON.parse(sessionStorage.getItem(JOB_SUBMISSIONS_KEY)||'null');
    if(submissions?.schema===1&&Array.isArray(submissions.items)){
      submissions.items.slice(0,50).forEach(item=>{
        if(item&&typeof item.signature==='string'&&/^[0-9a-f-]{32,36}$/.test(String(item.key))){
          jobSubmissionKeys.set(item.signature,{key:String(item.key),jobId:String(item.jobId||'')});
        }
      });
    }
  }catch(error){}
}

function persistJobRecoveryState(){
  try{
    localStorage.setItem(JOB_TRACKING_KEY,JSON.stringify({
      schema:1,ids:[...trackedJobIds].slice(-50),updated_at:new Date().toISOString()
    }));
  }catch(error){}
  try{
    sessionStorage.setItem(JOB_SUBMISSIONS_KEY,JSON.stringify({
      schema:1,
      items:[...jobSubmissionKeys].map(([signature,value])=>({signature,...value})).slice(-50)
    }));
  }catch(error){}
}

function submissionSignature(kind,payload){
  if(payload instanceof FormData){
    const parts=[];
    for(const [name,value] of payload.entries()){
      parts.push(value instanceof File
        ?`${name}:file:${value.name}:${value.size}:${value.lastModified}`
        :`${name}:${String(value)}`);
    }
    return `${kind}|${parts.sort().join('|')}`;
  }
  return `${kind}|${JSON.stringify(payload)}`;
}

function newIdempotencyKey(){
  return globalThis.crypto?.randomUUID?.()||(
    Date.now().toString(16)+'-'+Math.random().toString(16).slice(2)+Math.random().toString(16).slice(2)
  ).slice(0,36);
}

loadJobRecoveryState();

function normalizeJob(payload){
  const status=String(payload?.status||payload?.state||'queued');
  const completed=Math.max(0,safeInteger(payload?.completed_steps??payload?.current??payload?.progress?.current));
  const total=Math.max(0,safeInteger(payload?.total_steps??payload?.total??payload?.progress?.total));
  return {
    ...payload,
    id:String(payload?.id||payload?.job_id||''),
    status,
    stage:String(payload?.stage||payload?.message||payload?.progress?.message||''),
    completedSteps:completed,
    totalSteps:total,
    error:payload?.error||null
  };
}

function jobLabel(job){
  const labels={analysis:'错题分析',classification:'题目录入',ocr:'文档识别','batch-mistakes':'批量标错','exam-generation':'试卷生成','exam-recompile':'试卷重新编译','data-import':'数据导入','mistake-export':'错题 PDF 导出','memory-maintenance':'记忆维护','question-bank-rebuild':'题库索引重建'};
  return labels[job.kind||job.type]||'后台任务';
}

function jobStatusLabel(status){
  return {queued:'等待中',running:'进行中',cancel_requested:'正在取消',checkpointed:'已保存进度',published_pending_receipt:'已发布，等待回执确认',cancelled:'已取消',succeeded:'已完成',failed:'失败',interrupted:'已中断'}[status]||status;
}

function jobStatusHint(status){
  return {checkpointed:'任务已保存进度，后台将继续处理。',published_pending_receipt:'内容已发布，正在确认发布回执，请勿重复提交。'}[status]||'';
}

// User-facing failure copy is derived only from the structured code/category
// and job status. Raw backend message/hint/reason text can carry SQL, stack
// fragments, diagnostic IDs or mis-decoded bytes, so it is never displayed.
const JOB_ERROR_CODE_COPY=Object.freeze({
  candidate_pool_insufficient:['可用候选题不足，无法凑满题量。','可以减少题量或放宽章节后重新生成。'],
  candidate_budget_insufficient:['候选题的数量或分值不满足本次设置。','可以减少题量或放宽章节后重新生成。'],
  past_candidate_pool_insufficient:['所选题源中的可用真题不足。','可以更换题源或结构年份后重新生成。'],
  past_candidate_budget_insufficient:['所选题源中的可用真题不足。','可以更换题源或结构年份后重新生成。'],
  past_selection_constraint:['所选题源与结构年份无法同时满足组卷规则。','可以更换题源或结构年份后重新生成。'],
  past_difficulty_review_invalid:['真题难度核对未通过，本次没有生成试卷。','可以更换题源或结构年份后重新生成。'],
  no_usable_chapters:['题库中暂无可用于组卷的章节。','请先重建题库索引。'],
  hard_constraint_conflict:['组卷条件之间相互冲突，无法同时满足。','请调整题量、章节或题源后重新生成。'],
  question_bank_building:['题库索引正在重建。','请等待重建完成后再试。'],
  ocr_file_invalid:['文件无法识别。','请检查文件格式后重新上传。'],
  ocr_file_missing:['找不到上传的文件。','请重新上传文件。'],
  import_file_invalid:['导入文件无法读取。','请确认选择的是本应用导出的备份文件。'],
  invalid_import:['导入文件内容无效。','请确认选择的是本应用导出的备份文件。'],
  import_preview_required:['导入前需要先完成预览确认。','请重新选择文件并确认预览。'],
  vision_ocr_configuration:['文档识别服务尚未配置。','请在 AI 设置中完成配置后重试。'],
  configuration:['AI 或本地组件设置不完整。','请在 AI 设置中检查配置后重试。'],
  vision_ocr_provider:['文档识别服务暂时没有返回有效结果。','可以稍后重试。'],
  ai_provider_response:['AI 服务暂时没有返回有效结果。','可以稍后重试；多次失败请检查 AI 设置。'],
  context_safety_exceeded:['内容过长，超出了 AI 单次可处理的范围。','请减少内容后重新提交。'],
  material_understanding_incomplete:['材料未能完整识别。','请检查文件清晰度后重试。'],
  publication_failed:['结果保存未完成。','可以稍后重试。'],
  receipt_pending:['结果已提交，正在确认保存状态。','请稍候，不要重复提交。'],
  job_input_hash_mismatch:['任务内容校验未通过。','请重新提交该任务。']
});
const JOB_ERROR_CATEGORY_COPY=Object.freeze({
  input:['提交的内容或文件无效。','请检查后重新提交。'],
  configuration:['AI 或本地组件设置不完整。','请在 AI 设置中检查配置后重试。'],
  provider:['AI 服务暂时无法完成请求。','可以稍后重试；多次失败请检查 AI 设置。'],
  dependency:['所需的本地组件或数据暂时不可用。','可以稍后重试。'],
  temporary:['服务暂时繁忙。','可以稍后重试。'],
  business:['当前条件下无法完成该任务。','请调整设置后重试。'],
  storage:['本地数据读写没有成功。','可以稍后重试；反复出现时请检查磁盘空间或重启应用。'],
  data:['本地数据读写没有成功。','可以稍后重试；反复出现时请检查磁盘空间或重启应用。']
});
const JOB_ERROR_FALLBACK_COPY=Object.freeze(['任务未能完成。','可以重试；反复失败时请重启应用后再试。']);

function jobErrorCopy(job){
  const status=String(job?.status||'');
  if(status==='cancelled')return {message:'任务已取消。',hint:''};
  if(status==='interrupted')return {message:'任务意外中断，可能是应用曾关闭或重启。',hint:'可以重试继续处理。'};
  const error=job?.error&&typeof job.error==='object'?job.error:{};
  const code=String(error.code||'').toLowerCase();
  const category=String(error.category||'').toLowerCase();
  const [message,hint]=Object.hasOwn(JOB_ERROR_CODE_COPY,code)?JOB_ERROR_CODE_COPY[code]
    :(Object.hasOwn(JOB_ERROR_CATEGORY_COPY,category)?JOB_ERROR_CATEGORY_COPY[category]:JOB_ERROR_FALLBACK_COPY);
  return {message,hint};
}

// Deferred dependency waits are requeued with category=dependency; they are
// normal waiting, not a failed attempt awaiting automatic retry.
const JOB_DEPENDENCY_WAIT_COPY=Object.freeze({
  question_bank_building:'正在等待题库索引重建完成，完成后会自动继续。',
  memory_delivery_pending:'正在等待 AI 记忆准备完成，完成后会自动继续。',
  receipt_pending:'正在等待保存确认，完成后会自动继续。'
});

function jobWaitingDependency(job){
  return job?.status==='queued'&&String(job.error?.category||'').toLowerCase()==='dependency';
}

function jobWaitingAutoRetry(job){
  return job?.status==='queued'&&Boolean(job.error)&&!jobWaitingDependency(job);
}

// Progress text from the backend is shown only while a task is active, and
// only when it looks like plain prose rather than a leaked internal message.
function jobPlainStage(text){
  const value=String(text||'').trim();
  if(!value||value.length>80)return '';
  if(/[�{}<>[\]`\\]|Traceback|Exception|Error\b|sqlite|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bFROM\b|\bWHERE\b|[A-Za-z0-9_]+\.py\b|0x[0-9a-f]|[0-9a-f]{16,}/i.test(value))return '';
  return value;
}

function jobFailure(job){
  // The original backend error stays available on error.job.error.
  const source=job?.error;
  const copy=jobErrorCopy(job);
  const data={
    ...(source&&typeof source==='object'?source:{}),
    job_id:String(job?.id||''),
    kind:String(job?.kind||''),
    message:copy.message,
    hint:copy.hint
  };
  const error=new Error(copy.message);
  error.name='JobError';
  error.code=String(data.code||'job_failed');
  error.data=data;
  error.job=job;
  error.retryable=Boolean(data.retryable);
  return error;
}

const JOB_EXAM_ACTIONS=Object.freeze({
  reduce_question_count:{label:'减少题量',page:'exam',mode:'free',target:'e-count'},
  adjust_chapters:{label:'调整章节',page:'exam',mode:'free',target:'e-subject'},
  adjust_candidate_source:{label:'调整题源',page:'exam',mode:'past',target:'ep-candidate-source'},
  adjust_reference_year:{label:'调整结构年份',page:'exam',mode:'past',target:'ep-reference-year'},
  rebuild_question_bank:{label:'前往重建题库',page:'settings',target:'question-bank-rebuild'}
});
const LEGACY_EXAM_ERROR_ACTIONS=Object.freeze({
  candidate_pool_insufficient:['reduce_question_count','adjust_chapters','rebuild_question_bank'],
  candidate_budget_insufficient:['reduce_question_count','adjust_chapters'],
  past_candidate_pool_insufficient:['adjust_candidate_source','adjust_reference_year'],
  past_candidate_budget_insufficient:['adjust_candidate_source','adjust_reference_year'],
  past_selection_constraint:['adjust_candidate_source','adjust_reference_year'],
  past_difficulty_review_invalid:['adjust_candidate_source','adjust_reference_year'],
  no_usable_chapters:['rebuild_question_bank'],
  hard_constraint_conflict:['reduce_question_count','adjust_chapters','adjust_candidate_source']
});

function jobExamActions(job){
  if(job?.kind!=='exam-generation')return [];
  const supplied=Array.isArray(job?.error?.actions)?job.error.actions:[];
  const code=String(job?.error?.code||'').toLowerCase();
  const source=supplied.length?supplied:(Object.hasOwn(LEGACY_EXAM_ERROR_ACTIONS,code)?LEGACY_EXAM_ERROR_ACTIONS[code]:[]);
  return [...new Set(source.map(value=>String(value||'').trim()))]
    .filter(action=>Object.hasOwn(JOB_EXAM_ACTIONS,action));
}

function jobHasAdjustableExamError(job){
  return jobExamActions(job).length>0;
}

function openJobExamAction(action,button){
  const config=JOB_EXAM_ACTIONS[action];
  if(!config||button?.disabled)return;
  if(button){button.disabled=true;button.setAttribute('aria-busy','true')}
  setTaskPanel(false);
  nav(config.page);
  void window.Cuoti.features.load(config.page).then(feature=>{
    if(config.mode)feature.switchMode?.(config.mode);
    document.getElementById(config.target)?.focus();
  }).catch(()=>showToast('无法打开调整页面，请手动前往对应页面修改。','error'));
}

// Visual state per job: terminal and waiting states never get a progress bar
// or animation; only work that is actually running shows progress.
function jobDisplayState(job){
  if(job.status==='succeeded')return {tone:'done',icon:'circleCheck',label:'已完成',text:'任务已完成。'};
  if(job.status==='failed')return {tone:'failed',icon:'circleX',label:'失败'};
  if(job.status==='cancelled')return {tone:'muted',icon:'x',label:'已取消',text:'任务已取消，没有继续处理。'};
  if(job.status==='interrupted')return {tone:'warn',icon:'circleAlert',label:'已中断'};
  if(jobWaitingDependency(job)){
    const code=String(job.error?.code||'').toLowerCase();
    return {tone:'waiting',icon:'clock',label:'等待准备完成',
      text:Object.hasOwn(JOB_DEPENDENCY_WAIT_COPY,code)?JOB_DEPENDENCY_WAIT_COPY[code]:'正在等待所需内容准备完成，完成后会自动继续。'};
  }
  if(jobWaitingAutoRetry(job)){
    const used=Math.max(0,safeInteger(job.attempts_in_cycle));
    const max=Math.max(0,safeInteger(job.max_attempts));
    return {tone:'waiting',icon:'clock',label:'等待自动重试',
      text:`上次尝试未成功，稍后将自动重试${used&&max?`（已尝试 ${used}/${max} 次）`:''}。`};
  }
  if(job.status==='queued')return {tone:'waiting',icon:'clock',label:'排队中',text:'等待开始处理。'};
  if(job.status==='cancel_requested')return {tone:'waiting',icon:'loader',spin:true,label:'正在取消',text:'正在停止任务，请稍候。'};
  // These states have their own trusted copy; the last stage message may be
  // from an earlier phase (e.g. still "generating"), so it is not shown.
  if(job.status==='checkpointed'||job.status==='published_pending_receipt'){
    return {tone:'waiting',icon:'clock',label:jobStatusLabel(job.status),text:jobStatusHint(job.status)};
  }
  return {tone:'active',icon:'activity',label:jobStatusLabel(job.status),progress:true,
    text:jobPlainStage(job.stage)||jobStatusHint(job.status)||'正在处理。'};
}

function jobProgressHTML(job){
  const determinate=job.totalSteps>0;
  const percent=determinate?Math.min(100,Math.round(job.completedSteps/job.totalSteps*100)):0;
  return `<div class="task-progress${determinate?'':' indeterminate'}" role="progressbar" aria-label="任务进度" ${determinate?`aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"`:'aria-valuetext="进行中"'}><span style="${determinate?`width:${percent}%`:''}"></span></div>`;
}

function taskItemHTML(job){
    const state=jobDisplayState(job);
    const failed=['failed','interrupted'].includes(job.status);
    const copy=failed?jobErrorCopy(job):null;
    const examActions=jobExamActions(job);
    const errorCategory=String(job.error?.category||'');
    const manualRetryAllowed=['failed','interrupted','cancelled'].includes(job.status)
      &&!['input'].includes(errorCategory)
      &&!(job.kind==='ocr'&&job.status==='cancelled');
    const hint=!copy?'':(examActions.length?'可使用下方按钮调整后重新生成。':(manualRetryAllowed||!copy.hint.includes('重试')?copy.hint:''));
    return `<article class="task-item task-${escapeHTML(job.status)} task-tone-${state.tone}" data-job-id="${escapeHTML(job.id)}">
      <div class="task-item-head"><strong>${escapeHTML(jobLabel(job))}</strong><span class="task-state">${icon(state.icon,state.spin?'icon-spin':'')}${escapeHTML(state.label)}</span></div>
      <div class="task-stage">${escapeHTML(copy?copy.message:state.text)}</div>
      ${hint?`<div class="task-hint">${escapeHTML(hint)}</div>`:''}
      ${state.progress?jobProgressHTML(job):''}
      <div class="task-item-actions">
        ${JOB_CANCEL_ACTION_STATES.has(job.status)?`<button type="button" class="btn btn-out btn-xs js-job-cancel" data-job-id="${escapeHTML(job.id)}" ${job.status==='cancel_requested'?'disabled':''}>${job.status==='cancel_requested'?'正在取消':'取消'}</button>`:''}
        ${examActions.map(action=>`<button type="button" class="btn btn-p btn-xs js-job-adjust-exam" data-job-action="${escapeHTML(action)}">${escapeHTML(JOB_EXAM_ACTIONS[action].label)}</button>`).join('')}
        ${manualRetryAllowed?`<button type="button" class="btn btn-p btn-xs js-job-retry" data-job-id="${escapeHTML(job.id)}">重试</button>`:''}
      </div>
    </article>`;
}

function bindTaskItem(item){
  item.querySelectorAll('.js-job-cancel').forEach(button=>button.addEventListener('click',()=>cancelJob(button.dataset.jobId)));
  item.querySelectorAll('.js-job-adjust-exam').forEach(button=>button.addEventListener(
    'click',()=>openJobExamAction(button.dataset.jobAction,button)
  ));
  item.querySelectorAll('.js-job-retry').forEach(button=>button.addEventListener('click',()=>retryJob(button.dataset.jobId)));
}

function renderJobs(){
  const list=document.getElementById('task-list');
  const count=document.getElementById('task-count');
  if(!list||!count)return;
  const jobs=[...jobStore.values()].sort((a,b)=>String(b.updated_at||b.created_at||'').localeCompare(String(a.updated_at||a.created_at||'')));
  const activeJobs=jobs.filter(job=>ACTIVE_JOB_STATES.has(job.status));
  // Keep the drawer useful after long sessions: every active task remains
  // visible, while terminal history is capped to the latest 50 rows.
  const visibleJobs=[...activeJobs,...jobs.filter(job=>TERMINAL_JOB_STATES.has(job.status)).slice(0,50)]
    .sort((a,b)=>String(b.updated_at||b.created_at||'').localeCompare(String(a.updated_at||a.created_at||'')));
  const active=activeJobs.length;
  count.hidden=!active;
  count.textContent=String(active);
  if(!visibleJobs.length){list.innerHTML=emptyStateHTML('当前没有后台任务',{icon:'activity',description:'导入、分析和组卷任务会显示在这里。',compact:true});return}
  list.querySelector('.empty-state')?.remove();
  const focused=document.activeElement?.closest?.('[data-job-id]');
  const focusJobId=focused?.dataset.jobId||'';
  const focusAction=document.activeElement?.className||'';
  const existing=new Map([...list.querySelectorAll(':scope > [data-job-id]')]
    .map(item=>[item.dataset.jobId,item]));
  const desiredIds=new Set();
  visibleJobs.forEach((job,index)=>{
    desiredIds.add(job.id);
    const template=document.createElement('template');
    template.innerHTML=taskItemHTML(job);
    const candidate=template.content.firstElementChild;
    const renderKey=JSON.stringify([
      job.status,job.completedSteps,job.totalSteps,job.stage,job.updated_at,
      job.error,job.attempts,job.attempts_in_cycle,job.manual_retry_count
    ]);
    candidate.dataset.renderKey=renderKey;
    const current=existing.get(job.id);
    let item=current;
    if(!current||current.dataset.renderKey!==renderKey){
      bindTaskItem(candidate);
      if(current)current.replaceWith(candidate);
      else{
        const fragment=document.createDocumentFragment();
        fragment.appendChild(candidate);
        list.insertBefore(fragment,list.children[index]||null);
      }
      item=candidate;
    }
    const expected=list.children[index];
    if(item!==expected)list.insertBefore(item,expected||null);
  });
  existing.forEach((item,id)=>{if(!desiredIds.has(id))item.remove()});
  if(focusJobId){
    const item=[...list.querySelectorAll(':scope > [data-job-id]')]
      .find(candidate=>candidate.dataset.jobId===focusJobId);
    const className=String(focusAction).split(/\s+/).find(name=>name.startsWith('js-job-'));
    item?.querySelector(className?`.${className}`:'button')?.focus();
  }
}

function settleJobWaiters(job){
  if(!TERMINAL_JOB_STATES.has(job.status))return;
  for(const [signature,value] of jobSubmissionKeys){
    if(value.jobId===job.id)jobSubmissionKeys.delete(signature);
  }
  persistJobRecoveryState();
  const waiters=jobWaiters.get(job.id)||[];
  jobWaiters.delete(job.id);
  waiters.forEach(({resolve,reject})=>{
    if(job.status==='succeeded')resolve(job);
    else reject(jobFailure(job));
  });
}

function rememberJob(payload){
  const job=normalizeJob(payload?.job||payload);
  if(!job.id)return null;
  trackedJobIds.add(job.id);
  const previous=jobStore.get(job.id);
  const merged={...previous,...job};
  jobStore.set(job.id,merged);
  settleJobWaiters(merged);
  renderJobs();
  persistJobRecoveryState();
  scheduleJobPolling();
  // Consumers such as the settings and exam pages can recover from a
  // completed dependency without coupling themselves to the polling loop.
  const changed=!previous||
    previous.status!==merged.status||
    previous.completedSteps!==merged.completedSteps||
    previous.stage!==merged.stage;
  if(changed){
    try{window.dispatchEvent(new CustomEvent('cuoti:job-update',{detail:merged}))}catch(error){}
  }
  return merged;
}

async function fetchJob(id){
  const job=rememberJob(await api('/api/v2/jobs/'+encodeURIComponent(id)));
  return job;
}

async function pollJobs(){
  jobPollTimer=null;
  if(!jobsEndpointAvailable||jobPollInFlight)return;
  if(document.hidden){scheduleJobPolling();return}
  jobPollInFlight=true;
  const active=[...jobStore.values()].filter(job=>ACTIVE_JOB_STATES.has(job.status));
  try{
    const missing=[...trackedJobIds].filter(id=>!jobStore.has(id));
    if(active.length||missing.length){
      // Refresh the active set in one request. An active job that disappears
      // from this response is checked individually so terminal transitions
      // still settle waiters without requiring one request per active job.
      let listedActiveIds=new Set();
      if(active.length){
        try{
          const payload=await fetchJobCollection('/api/v2/jobs?state=active&limit=200');
          const jobs=payload===null?active:(Array.isArray(payload)?payload:(payload?.items||payload?.jobs||[]));
          jobs.forEach(job=>{
            const remembered=rememberJob(job);
            if(remembered)listedActiveIds.add(remembered.id);
          });
        }catch(error){
          if(error.status!==404)throw error;
          // Older deployments may not expose the collection filter; retain
          // the previous per-job behavior as a compatibility fallback.
          listedActiveIds=new Set(active.map(job=>job.id));
          await Promise.all(active.map(job=>fetchJob(job.id)));
        }
      }
      await Promise.all([
        ...active
          .filter(job=>!listedActiveIds.has(job.id))
          .map(job=>fetchJob(job.id)),
        ...missing.slice(0,50).map(id=>fetchJob(id).catch(error=>{
          if(error.status===404)trackedJobIds.delete(id);else throw error;
        }))
      ]);
      setTaskPanelStatus('');
    }else{
      const collectionURL=jobInitialHistoryLoaded?'/api/v2/jobs?state=active':'/api/v2/jobs?limit=50';
      const payload=await fetchJobCollection(collectionURL);
      jobInitialHistoryLoaded=true;
      setTaskPanelStatus('');
      if(payload===null)return;
      const jobs=Array.isArray(payload)?payload:(payload?.items||payload?.jobs||[]);
      jobs.forEach(rememberJob);
    }
  }catch(error){
    if(error.status===404)jobsEndpointAvailable=false;
    else setTaskPanelStatus('任务状态暂时无法刷新，稍后会自动再试；请确认应用仍在运行。');
  }finally{
    jobPollInFlight=false;
  }
  scheduleJobPolling();
}

function scheduleJobPolling(){
  if(jobPollTimer||!jobsEndpointAvailable)return;
  const hasActive=[...jobStore.values()].some(job=>ACTIVE_JOB_STATES.has(job.status));
  if(document.hidden&&!hasActive)return;
  const delay=document.hidden?30000:(hasActive?1200:10000);
  jobPollTimer=window.setTimeout(pollJobs,delay);
}

function refreshJobPolling(){
  if(jobPollTimer){window.clearTimeout(jobPollTimer);jobPollTimer=null}
  if(!document.hidden&&!jobPollInFlight)void pollJobs();
  else scheduleJobPolling();
}

function waitForJob(id){
  const known=jobStore.get(String(id));
  if(known&&TERMINAL_JOB_STATES.has(known.status)){
    if(known.status==='succeeded')return Promise.resolve(known);
    return Promise.reject(jobFailure(known));
  }
  return new Promise((resolve,reject)=>{
    const key=String(id);
    const waiters=jobWaiters.get(key)||[];
    waiters.push({resolve,reject});
    jobWaiters.set(key,waiters);
    scheduleJobPolling();
  });
}

async function jobResult(job){
  if(job.result!=null)return job.result;
  const payload=await api('/api/v2/jobs/'+encodeURIComponent(job.id)+'/result');
  return payload?.result??payload;
}

async function resolveJobResponse(payload){
  const job=rememberJob(payload);
  if(!job)return payload;
  const completed=await waitForJob(job.id);
  return jobResult(completed);
}

async function runJobRequest(kind,payload){
  const signature=submissionSignature(kind,payload);
  let submission=jobSubmissionKeys.get(signature);
  if(!submission){
    submission={key:newIdempotencyKey(),jobId:''};
    jobSubmissionKeys.set(signature,submission);
    persistJobRecoveryState();
  }
  const created=await api('/api/v2/jobs/'+encodeURIComponent(kind),{
    method:'POST',
    headers:{'Idempotency-Key':submission.key},
    body:payload instanceof FormData?payload:JSON.stringify(payload)
  });
  const submitted=normalizeJob(created?.job||created);
  submission.jobId=submitted.id;
  jobSubmissionKeys.set(signature,submission);
  persistJobRecoveryState();
  return resolveJobResponse(created);
}

function trackJob(payload){
  const job=rememberJob(payload);
  if(job)refreshJobPolling();
  return job;
}

function subscribeJob(id,callback){
  const key=String(id||'');
  if(!key)return Promise.reject(new Error('任务 ID 无效'));
  const promise=waitForJob(key);
  if(typeof callback==='function')promise.then(callback).catch(()=>{});
  refreshJobPolling();
  return promise;
}

function setTaskPanelStatus(text){
  const status=document.getElementById('task-panel-status');
  if(status)status.textContent=text;
}

async function cancelJob(id){
  try{
    setTaskPanelStatus('');
    rememberJob(await api('/api/v2/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'}));
  }catch(error){
    setTaskPanelStatus(error?.status===409?'任务状态已变化，无法取消，请查看最新状态。':'取消没有成功，请稍后再试。');
  }
}

async function retryJob(id){
  try{
    setTaskPanelStatus('');
    rememberJob(await api('/api/v2/jobs/'+encodeURIComponent(id)+'/retry',{method:'POST'}));
  }catch(error){
    setTaskPanelStatus(error?.status===409?'该任务当前不能重试，可能已在处理中。':'重试没有成功，请稍后再试。');
  }
}

let taskPanelPreviousFocus=null;

function taskPanelFocusableElements(panel){
  return [...panel.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
    .filter(element=>element.offsetParent!==null);
}

function setTaskPanelBackgroundInert(inert){
  document.querySelectorAll('.main,.sb').forEach(element=>{
    if(inert)element.setAttribute('inert','');
    else element.removeAttribute('inert');
  });
}

function renderTaskPanel(open){
  const panel=document.getElementById('task-panel');
  const scrim=document.getElementById('task-panel-scrim');
  const toggle=document.getElementById('task-panel-toggle');
  if(!panel||!scrim||!toggle)return;
  const wasOpen=!panel.hidden;
  if(open&&!wasOpen)taskPanelPreviousFocus=document.activeElement;
  panel.hidden=!open;
  scrim.hidden=!open;
  toggle.setAttribute('aria-expanded',String(open));
  setTaskPanelBackgroundInert(open);
  if(open)document.getElementById('task-panel-close')?.focus();
  else{
    const restore=taskPanelPreviousFocus;
    taskPanelPreviousFocus=null;
    (restore?.isConnected?restore:toggle).focus();
  }
}

function setTaskPanel(open){
  if(open){
    renderTaskPanel(true);
    window.pushHistoryLayer?.('task-panel',()=>renderTaskPanel(false));
    return;
  }
  if(!window.dismissHistoryLayer?.('task-panel'))renderTaskPanel(false);
}

document.getElementById('task-panel-toggle')?.addEventListener('click',()=>setTaskPanel(true));
document.getElementById('task-panel-close')?.addEventListener('click',()=>setTaskPanel(false));
document.getElementById('task-panel-scrim')?.addEventListener('click',()=>setTaskPanel(false));
document.addEventListener('keydown',event=>{
  const panel=document.getElementById('task-panel');
  if(panel?.hidden)return;
  if(event.key==='Escape'){setTaskPanel(false);return}
  if(event.key!=='Tab')return;
  const focusable=taskPanelFocusableElements(panel);
  if(!focusable.length){event.preventDefault();panel.focus();return}
  const first=focusable[0],last=focusable[focusable.length-1];
  if(!panel.contains(document.activeElement)){
    event.preventDefault();(event.shiftKey?last:first).focus();
  }else if(event.shiftKey&&document.activeElement===first){
    event.preventDefault();last.focus();
  }else if(!event.shiftKey&&document.activeElement===last){
    event.preventDefault();first.focus();
  }
});
document.addEventListener('visibilitychange',refreshJobPolling);
window.addEventListener('online',refreshJobPolling);

window.runJobRequest=runJobRequest;
window.trackJob=trackJob;
window.subscribeJob=subscribeJob;
window.refreshJobs=refreshJobPolling;
void pollJobs();
