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

function jobFailure(job){
  const source=job?.error;
  const data=source&&typeof source==='object'
    ?{...source,job_id:String(job.id||''),kind:String(job.kind||'')}
    :{message:String(source||jobStatusLabel(job?.status)),job_id:String(job?.id||''),kind:String(job?.kind||'')};
  const error=new Error(String(data.message||jobStatusLabel(job?.status)));
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
  const source=supplied.length?supplied:(LEGACY_EXAM_ERROR_ACTIONS[code]||[]);
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
  }).catch(error=>showToast(`无法打开调整页面：${error.message}`,'error'));
}

function taskItemHTML(job){
    const determinate=job.totalSteps>0;
    const percent=determinate?Math.min(100,Math.round(job.completedSteps/job.totalSteps*100)):0;
    const errorMessage=typeof job.error==='string'?job.error:job.error?.message;
    const errorHint=typeof job.error?.hint==='string'?job.error.hint.trim():'';
    const examActions=jobExamActions(job);
    const errorCategory=String(job.error?.category||'');
    const manualRetryAllowed=['failed','interrupted','cancelled'].includes(job.status)
      &&!['input'].includes(errorCategory)
      &&!(job.kind==='ocr'&&job.status==='cancelled');
    const laneLabel=job.lane==='fast'?'快速通道':(job.lane==='heavy'?'重型通道':'');
    const attemptLabel=`尝试 ${Math.max(0,safeInteger(job.attempts))} 次`+
      (safeInteger(job.manual_retry_count)>0?` · 手动重试 ${safeInteger(job.manual_retry_count)} 次`:``);
    const correlation=String(job.error?.correlation_id||'');
    return `<article class="task-item task-${escapeHTML(job.status)}" data-job-id="${escapeHTML(job.id)}">
      <div class="task-item-head"><strong>${escapeHTML(jobLabel(job))}</strong><span>${escapeHTML(jobStatusLabel(job.status))}</span></div>
      <div class="task-stage">${escapeHTML(errorMessage||job.stage||jobStatusHint(job.status))}</div>
      ${errorHint&&errorHint!==errorMessage?`<div class="task-hint">${escapeHTML(errorHint)}</div>`:''}
      <div class="task-meta">${escapeHTML([laneLabel,attemptLabel].filter(Boolean).join(' · '))}${correlation?` · 诊断编号 ${escapeHTML(correlation)}`:''}</div>
      <div class="task-progress${determinate?'':' indeterminate'}" role="progressbar" ${determinate?`aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"`:'aria-label="任务进行中"'}><span style="${determinate?`width:${percent}%`:''}"></span></div>
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
  if(!visibleJobs.length){list.innerHTML=emptyStateHTML('当前没有后台任务',{icon:'history',description:'导入、分析和组卷任务会显示在这里。',compact:true});return}
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
      job.error,job.attempts,job.manual_retry_count
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
    }else{
      const collectionURL=jobInitialHistoryLoaded?'/api/v2/jobs?state=active':'/api/v2/jobs?limit=50';
      const payload=await fetchJobCollection(collectionURL);
      jobInitialHistoryLoaded=true;
      if(payload===null)return;
      const jobs=Array.isArray(payload)?payload:(payload?.items||payload?.jobs||[]);
      jobs.forEach(rememberJob);
    }
  }catch(error){
    if(error.status===404)jobsEndpointAvailable=false;
    else document.getElementById('task-panel-status').textContent='任务状态暂时无法刷新：'+error.message;
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

async function cancelJob(id){
  try{
    rememberJob(await api('/api/v2/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'}));
  }catch(error){
    document.getElementById('task-panel-status').textContent='取消失败：'+error.message;
  }
}

async function retryJob(id){
  try{
    rememberJob(await api('/api/v2/jobs/'+encodeURIComponent(id)+'/retry',{method:'POST'}));
  }catch(error){
    document.getElementById('task-panel-status').textContent='重试失败：'+error.message;
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
