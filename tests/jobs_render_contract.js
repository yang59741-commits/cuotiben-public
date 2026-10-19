'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const privateText='malformed database schema (revision_exam_attempts_insert) Traceback INTERNAL_EVENT �乱码';
const statusNode={textContent:''};
const toasts=[];
const context={
  console,URLSearchParams,Headers,FormData,
  document:{hidden:true,getElementById:id=>id==='task-panel-status'?statusNode:null,addEventListener(){}},
  localStorage:{getItem:()=>null,setItem(){}},sessionStorage:{getItem:()=>null,setItem(){}},
  safeInteger:value=>Number.isFinite(Number(value))?Math.trunc(Number(value)):0,
  escapeHTML:value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),
  setTimeout:()=>1,clearTimeout(){},addEventListener(){},dispatchEvent(){},
  CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail}},
  api:async()=>{throw new Error(privateText)},
  showToast:message=>toasts.push(message),nav(){},
};
context.window=context;
vm.createContext(context);
for(const file of ['icons.js','jobs.js'])vm.runInContext(fs.readFileSync(path.join(root,'cuoti_app/static/js',file),'utf8'),context);

function job(overrides={}){
  return context.normalizeJob({id:'fixture-job',kind:'exam-generation',status:'failed',lane:'heavy',attempts:1,
    progress:{current:2,total:7,message:privateText},
    error:{code:'http_500',category:'internal',message:privateText,hint:privateText,reason:privateText,correlation_id:'private-diagnostic-id'},
    ...overrides});
}
function assertPrivateTextHidden(text){
  for(const marker of ['malformed','revision_exam_attempts_insert','Traceback','INTERNAL_EVENT','�','private-diagnostic-id','重型通道','诊断编号','[object Object]','undefined']){
    assert.ok(!text.includes(marker),`Private diagnostic rendered: ${marker}`);
  }
}

(async()=>{
  for(const status of ['failed','interrupted','cancelled','succeeded']){
    for(const total of [0,7]){
      const html=context.taskItemHTML(job({status,progress:{current:2,total,message:privateText}}));
      assert.ok(!html.includes('role="progressbar"'),`${status} must not show a progressbar`);
      assert.ok(!html.includes('indeterminate'),`${status} must not animate`);
      assertPrivateTextHidden(html);
    }
  }
  for(const total of [0,7]){
    const html=context.taskItemHTML(job({status:'running',error:null,progress:{current:2,total,message:'正在生成试卷'}}));
    assert.ok(html.includes('role="progressbar"'));
    assert.equal(html.includes('indeterminate'),total===0);
    if(total)assert.ok(html.includes('aria-valuenow="29"'));
  }
  const queued=context.taskItemHTML(job({status:'queued'}));
  assertPrivateTextHidden(queued);
  assert.ok(queued.includes('等待')&&queued.includes('重试'));
  assert.ok(!queued.includes('indeterminate'));
  const dependency=context.taskItemHTML(job({status:'queued',error:{code:'question_bank_building',category:'dependency',message:privateText}}));
  assertPrivateTextHidden(dependency);
  assert.ok(dependency.includes('等待'));
  assert.ok(!dependency.includes('上次尝试未成功'));
  assert.ok(!dependency.includes('indeterminate'));
  for(const status of ['checkpointed','published_pending_receipt']){
    const html=context.taskItemHTML(job({status}));
    assertPrivateTextHidden(html);
    assert.ok(!html.includes('js-job-retry'));
  }
  const published=context.taskItemHTML(job({status:'published_pending_receipt',stage:'正在生成试卷'}));
  assert.ok(!published.includes('正在生成试卷'));
  const unsafe=job();
  const before=JSON.stringify(unsafe);
  const failure=context.jobFailure(unsafe);
  assertPrivateTextHidden(failure.message);
  assertPrivateTextHidden(String(failure.data.hint||''));
  assert.equal(failure.code,'http_500');
  assert.equal(failure.retryable,false);
  assert.equal(JSON.stringify(unsafe),before,'Presentation must not rewrite source diagnostics');
  assertPrivateTextHidden(context.taskItemHTML(job({error:privateText})));
  for(const code of ['new_unrecognized_code','constructor','__proto__']){
    assertPrivateTextHidden(context.taskItemHTML(job({error:{code,category:'constructor',message:privateText}})));
  }
  const actionable=job({error:{code:'past_selection_constraint',category:'input',message:privateText,hint:privateText,actions:['adjust_candidate_source','adjust_reference_year',privateText]}});
  const actionHTML=context.taskItemHTML(actionable);
  assertPrivateTextHidden(actionHTML);
  assert.ok(actionHTML.includes('data-job-action="adjust_candidate_source"'));
  assert.ok(actionHTML.includes('data-job-action="adjust_reference_year"'));
  assert.ok(!actionHTML.includes('js-job-retry'));
  assert.ok(context.taskItemHTML(job()).includes('js-job-retry'));
  assert.ok(!context.taskItemHTML(job({kind:'ocr',status:'cancelled'})).includes('js-job-retry'));

  context.rememberJob(job({id:'transition',status:'running',error:null}));
  const pending=context.waitForJob('transition').then(()=>assert.fail('Failed task resolved successfully'),error=>assertPrivateTextHidden(error.message));
  context.rememberJob(job({id:'transition',status:'failed'}));
  await pending;
  for(const action of [context.cancelJob,context.retryJob]){
    statusNode.textContent='';await action('fixture-job');
    assert.ok(statusNode.textContent);assertPrivateTextHidden(statusNode.textContent);
  }
  context.document.hidden=false;
  context.fetch=async()=>{throw new Error(privateText)};
  await context.pollJobs();
  assert.ok(statusNode.textContent);assertPrivateTextHidden(statusNode.textContent);
  console.log('JOBS_RENDER_CONTRACT_OK terminal/active/retry/privacy/actions/waiters');
})().catch(error=>{console.error(error);process.exitCode=1});
