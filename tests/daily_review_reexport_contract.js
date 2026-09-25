'use strict';

// Historical Daily Self-Review PDF re-export: exercises the real controller
// handler and history-row wiring against a small in-memory fixture.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..','cuoti_app','static','js');
const tick=async(count=3)=>{for(let i=0;i<count;i+=1)await new Promise(resolve=>setImmediate(resolve));};

function deferred(){let resolve,reject;const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail});return {promise,resolve,reject};}
function httpError(status,code,message='fixture error'){return Object.assign(new Error(message),{status,data:{code,error:message}});}
function jobError(status,code,message){
  return Object.assign(new Error(message),{name:'JobError',code,data:{code,message},job:{id:'j',status}});
}
function jobResult(overrides={}){
  return {id:42,export_context:'daily_review',is_reexport:true,source_session_id:7,daily_review_round_id:7,round_number:2,
    layout:'compact',item_count:3,completion_status:'completed',completed_count:3,legacy_live_content:false,
    download_url:'/mistake-export-batches/42.zip',download_name:'每日自主复习批次_42.zip',
    volumes:[{id:51,download_url:'/mistake-exports/51.pdf',download_name:'每日自主复习_第1册.pdf'}],...overrides};
}

function fixture(){
  const nodes=new Map(),timers=new Map(),windowEvents={};
  const posts=[],writes=[],reads=[],jobs=[],modals=[],toasts=[],downloads=[],tracked=[],subscriptions=[],resultReads=[];
  let serial=0,hidden=0;
  // History page: session 7 is round 2 of an older batch whose first PDF expired.
  const history=[
    {id:9,round_number:3,practice_mode:'mixed',selection:{pdf_purpose:'self_test'},item_count:2,completed_count:0,created_at:'2026-09-20T08:00:00'},
    {id:7,round_number:2,practice_mode:'mixed',selection:{pdf_purpose:'guided'},item_count:3,completed_count:3,created_at:'2026-08-01T08:00:00',export_batch_id:5}
  ];
  const session={id:41,round_id:41,round_number:5,selection:{},items:[1,2].map(id=>({
    id,round_id:41,subject_id:1,chapter_id:2,public_id:`p${id}`,completion_result:null,assistance:'unknown',content:'fixture'
  }))};
  const context={console,AbortController,URLSearchParams,Map,Set,Date,
    safeInteger:(v,f=0)=>Number.isSafeInteger(Number(v))&&String(v).trim()!==''?Number(v):f,
    escapeHTML:value=>String(value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),
    icon:name=>`<i data-icon="${name}"></i>`,problemPreviewHTML:String,emptyStateHTML:String,
    syncCustomSelect:()=>{},renderMath:async()=>true,
    safeLocalURL:url=>typeof url==='string'&&url.startsWith('/')&&!url.startsWith('//')?url:'',
    showModal:(...args)=>modals.push(args),hideModal:()=>{hidden+=1},
    showToast:(...args)=>toasts.push(args),
    localStorage:{getItem:()=>null,setItem:()=>{}},crypto:{randomUUID:()=>`key-${++serial}`},
    setTimeout:fn=>{const id=++serial;timers.set(id,fn);return id},
    clearTimeout:id=>timers.delete(id)
  };
  function element(id){
    if(nodes.has(id))return nodes.get(id);
    const node={id,hidden:id==='daily-drawer',dataset:{},style:{},value:'',options:[],isConnected:true,textContent:'',innerHTML:'',
      setAttribute:()=>{},removeAttribute:()=>{},getClientRects:()=>[{}],closest:()=>null,
      addEventListener:()=>{},querySelector:()=>null,querySelectorAll:()=>[],contains:()=>false,focus:()=>{},
      classList:{contains:name=>id==='p-review'&&name==='on',toggle:()=>{},add:()=>{},remove:()=>{}},
      appendChild:child=>{child.parentElement=node}
    };
    nodes.set(id,node);return node;
  }
  // Minimal history modal: the controller writes markup, then reads these nodes.
  const list={html:'',listeners:[],insertAdjacentHTML(_where,html){this.html+=html},addEventListener(name,fn){if(name==='click')this.listeners.push(fn)}};
  const more={hidden:false,disabled:false,addEventListener:()=>{}};
  const layoutSelect={value:'compact'};
  const modalBody=Object.assign(element('modal-body'),{querySelector:selector=>({'#daily-history-list':list,'#daily-history-more':more,'#daily-history-layout':layoutSelect})[selector]||null});
  context.document={body:element('body'),getElementById:element,querySelector:()=>null,querySelectorAll:()=>[],
    addEventListener:()=>{},activeElement:null,
    createElement:tag=>({tag,hidden:false,click(){downloads.push({href:this.href,download:this.download})},remove(){}})};
  context.window=context;context.Cuoti={};
  context.addEventListener=(name,fn)=>{windowEvents[name]=fn};
  context.api=(url,options={})=>{
    if(url.endsWith('/pdf')){const call={url,options,...deferred()};posts.push(call);return call.promise;}
    if(/\/items\/\d+\/result$/.test(url))return new Promise((resolve,reject)=>writes.push({url,options,resolve,reject}));
    if(/^\/api\/v2\/jobs\/[^/]+\/result$/.test(url)){const call={url,...deferred()};resultReads.push(call);return call.promise;}
    if(url.startsWith('/api/v2/daily-review/sessions?'))return Promise.resolve({items:history,has_more:false,next_cursor:null});
    const opened=url.match(/\/api\/v2\/daily-review\/sessions\/(\d+)$/);
    if(opened||url.includes('/current')){
      reads.push(url);
      const payload=JSON.parse(JSON.stringify(session));
      if(opened)Object.assign(payload,{id:Number(opened[1]),round_id:Number(opened[1])});
      return Promise.resolve(payload);
    }
    return Promise.resolve({items:[]});
  };
  context.runJobRequest=(kind,input)=>new Promise((resolve,reject)=>jobs.push({kind,input,resolve,reject}));
  context.trackJob=payload=>{tracked.push(payload);const job=payload?.job||payload;return job?.id?{...job,id:String(job.id)}:null};
  context.subscribeJob=id=>{const call={id,...deferred()};subscriptions.push(call);return call.promise;};
  vm.createContext(context);
  for(const name of ['state','api','render','controller']){
    vm.runInContext(fs.readFileSync(path.join(root,'daily-review',name+'.js'),'utf8'),context,{filename:name+'.js'});
  }
  const feature=context.Cuoti.dailyReview;
  feature.state.session=session;feature.state.items=session.items.map(feature.render.normalize);
  feature.controller.bind();
  const f={feature,context,posts,writes,reads,jobs,modals,toasts,downloads,tracked,subscriptions,resultReads,timers,list,layoutSelect,
    get hidden(){return hidden},
    // Resolve the most recent submission as a 202 (new) or 200 (replayed) job.
    accept(index=posts.length-1,id=`job-${index}`){posts[index].resolve({job_id:id,state:'queued',job:{id,status:'queued',kind:'mistake-export'}});return id},
    // Dispatch a click through the delegated history listener.
    click(target){
      const event={target,defaultPrevented:false,propagationStopped:false,
        preventDefault(){this.defaultPrevented=true},stopPropagation(){this.propagationStopped=true}};
      list.listeners.forEach(fn=>fn(event));return event;
    }
  };
  return f;
}

// Fake targets mirroring the rendered history markup.
function pdfTarget(id,{disabled=false,label=''}={}){
  const button={disabled,dataset:{sessionPdf:String(id),sessionLabel:label}};
  button.closest=selector=>selector==='[data-session-pdf]'?button:null;
  return button;
}
function rowTarget(id){
  const row={dataset:{session:String(id)}};
  const strong={closest:selector=>selector==='[data-session]'?row:null};
  return strong;
}
function body(call){return JSON.parse(call.options.body);}
function key(call){return call.options.headers['Idempotency-Key'];}

async function run(){
  let scenarios=0;
  // Markup: the row and the PDF action are siblings, never nested buttons.
  {
    const f=fixture();await f.feature.controller.history();
    let depth=0,maxDepth=0;
    for(const tag of f.list.html.match(/<\/?button\b/g)){depth+=tag.startsWith('</')?-1:1;maxDepth=Math.max(maxDepth,depth);}
    assert.equal(depth,0);assert.equal(maxDepth,1);
    assert.match(f.list.html,/data-session="7"[\s\S]*?<\/button><button type="button" class="btn btn-txt btn-sm" data-session-pdf="7"/);
    assert.equal(f.list.listeners.length,1);scenarios+=1;
  }
  // Click on a history row's PDF action: correct session id, layout body and key; 202 job to download.
  {
    const f=fixture();await f.feature.controller.history();await tick();
    const readsBefore=f.reads.length;
    f.layoutSelect.value='notes';
    const event=f.click(pdfTarget(7,{label:'全库第 2 轮 · 批次 7'}));
    f.layoutSelect.value='two_column'; // Later changes must not affect the captured request.
    assert.equal(event.propagationStopped,true);assert.equal(event.defaultPrevented,true);
    assert.equal(f.hidden,0);assert.equal(f.posts.length,1);
    const post=f.posts[0];
    assert.equal(post.url,'/api/v2/daily-review/sessions/7/pdf');
    assert.equal(post.options.method,'POST');
    assert.deepEqual(body(post),{layout:'notes'});
    assert.equal(key(post),'key-1');
    const id=f.accept();await tick();
    assert.equal(f.tracked.length,1);assert.equal(f.tracked[0].job_id,id);
    assert.equal(f.subscriptions[0].id,id);assert.equal(f.downloads.length,0);
    f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult({layout:'notes'})});await tick();
    assert.deepEqual(f.downloads,[{href:'/mistake-export-batches/42.zip',download:'每日自主复习批次_42.zip'}]);
    assert.equal(f.reads.length,readsBefore);assert.equal(f.jobs.length,0);
    assert.equal(f.feature.state.session.id,41);assert.equal(f.feature.state.sessionId,null);
    assert.equal(f.feature.state.reexports.size,0);assert.equal(f.feature.state.reexportKeys.size,0);
    assert.equal(f.modals.filter(args=>args[2]==='error').length,0);scenarios+=1;
  }
  // A 200 replay without an inline result reads /api/v2/jobs/{id}/result, then downloads.
  {
    const f=fixture();const pending=f.feature.controller.reexportPdf(7,'compact');await tick();
    const id=f.accept(0,'replayed');await tick();
    f.subscriptions[0].resolve({id,status:'succeeded'});await tick();
    assert.equal(f.resultReads[0].url,'/api/v2/jobs/replayed/result');
    f.resultReads[0].resolve({job_id:id,result:jobResult(),exam_ids:[]});
    const result=await pending;
    assert.equal(result.id,42);assert.equal(f.downloads.length,1);scenarios+=1;
  }
  // Immediate duplicate clicks enter once; the button is disabled while running.
  {
    const f=fixture();await f.feature.controller.history();await tick();
    const first=f.feature.controller.reexportPdf(7,'compact');
    f.click(pdfTarget(7));const second=f.feature.controller.reexportPdf('7','compact');
    assert.equal(f.posts.length,1);assert.equal(await second,null);
    assert.equal(f.feature.state.reexports.get(7).layout,'compact');
    await f.feature.controller.history();
    assert.match(f.list.html,/data-session-pdf="7"[^>]* disabled aria-busy="true"/);
    f.click(pdfTarget(7,{disabled:true}));assert.equal(f.posts.length,1);
    const id=f.accept();await tick();f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult()});await first;
    assert.equal(f.feature.state.reexports.size,0);
    // A new intentional export after completion uses a fresh key.
    void f.feature.controller.reexportPdf(7,'compact');assert.equal(f.posts.length,2);
    assert.notEqual(key(f.posts[1]),key(f.posts[0]));scenarios+=1;
  }
  // Uncertain submissions (network error, 5xx) keep the key for retry; a definitive outcome drops it.
  {
    const f=fixture();
    let pending=f.feature.controller.reexportPdf(7,'notes');f.posts[0].reject(new Error('无法连接到本地服务'));await pending;
    assert.ok(f.toasts.at(-1)[0].startsWith('PDF 重新生成未确认：'));assert.equal(f.feature.state.reexports.size,0);
    pending=f.feature.controller.reexportPdf(7,'notes');
    assert.equal(key(f.posts[1]),key(f.posts[0]));
    f.posts[1].reject(httpError(503,'SERVICE_UNAVAILABLE'));await pending;
    pending=f.feature.controller.reexportPdf(7,'notes');assert.equal(key(f.posts[2]),key(f.posts[0]));
    const id=f.accept(2);await tick();f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult()});await pending;
    assert.equal(f.downloads.length,1);
    void f.feature.controller.reexportPdf(7,'notes');assert.notEqual(key(f.posts[3]),key(f.posts[0]));
    // A different layout is a different action and never borrows the uncertain key.
    const g=fixture();pending=g.feature.controller.reexportPdf(7,'notes');g.posts[0].reject(new Error('offline'));await pending;
    void g.feature.controller.reexportPdf(7,'compact');assert.notEqual(key(g.posts[1]),key(g.posts[0]));
    scenarios+=1;
  }
  // A failed result read after a finished job is retried with the same key (replay of that job).
  {
    const f=fixture();let pending=f.feature.controller.reexportPdf(7,'compact');
    const id=f.accept();await tick();f.subscriptions[0].resolve({id,status:'succeeded'});await tick();
    f.resultReads[0].reject(new Error('offline'));await pending;
    assert.ok(f.toasts.at(-1)[0].startsWith('PDF 重新生成未确认：'));
    pending=f.feature.controller.reexportPdf(7,'compact');assert.equal(key(f.posts[1]),key(f.posts[0]));
    f.accept(1,id);await tick();f.subscriptions[1].resolve({id,status:'succeeded',result:jobResult()});await pending;
    assert.equal(f.downloads.length,1);scenarios+=1;
  }
  // Submission errors (404/409) are shown, drop the key, and never fall back to a new batch.
  for(const [status,code,hint] of [[404,'ROUND_NOT_FOUND','不存在'],[409,'DAILY_REVIEW_CONFLICT','已发布或已完成'],[409,'DAILY_REVIEW_SNAPSHOT_UNAVAILABLE','不会改用当前题目内容'],[400,'INVALID_LAYOUT','']]){
    const f=fixture();const pending=f.feature.controller.reexportPdf(7,'compact');
    f.posts[0].reject(httpError(status,code,'服务端拒绝'));assert.equal(await pending,null);
    const [message,type,options]=f.toasts.at(-1);
    assert.ok(message.startsWith('PDF 重新生成失败：'));assert.equal(type,'error');
    assert.equal(options.persistent,true);
    assert.ok(message.includes('服务端拒绝')&&message.includes(hint),message);
    assert.equal(f.subscriptions.length,0);assert.equal(f.jobs.length,0);assert.equal(f.reads.length,0);
    assert.equal(f.feature.state.sessionId,null);assert.equal(f.feature.state.session.id,41);
    assert.equal(f.feature.state.reexportKeys.size,0);assert.equal(f.downloads.length,0);
  }
  scenarios+=1;
  // Worker-side failures: snapshot recheck, compilation, cancellation, interruption.
  for(const [status,code,title,text] of [
    ['failed','daily_review_snapshot_unavailable','PDF 重新生成失败','不会改用当前题目内容'],
    ['failed','xelatex_failed','PDF 编译失败','XeLaTeX'],
    ['cancelled','cancelled','PDF 重新生成已取消','已取消'],
    ['interrupted','job_interrupted','PDF 重新生成中断','任务面板']
  ]){
    const f=fixture();const pending=f.feature.controller.reexportPdf(7,'compact');
    f.accept();await tick();f.subscriptions[0].reject(jobError(status,code,'XeLaTeX 编译失败'));
    assert.equal(await pending,null);
    const [message,type,options]=f.toasts.at(-1);
    assert.ok(message.startsWith(title+'：'));assert.equal(type,'error');assert.ok(message.includes(text),message);
    assert.equal(options.persistent,true);
    assert.equal(f.feature.state.reexportKeys.size,0);assert.equal(f.downloads.length,0);
    assert.equal(f.jobs.length,0);assert.equal(f.reads.length,0);
  }
  scenarios+=1;
  // Changing the displayed session during compilation: the export stays bound to its target.
  {
    const f=fixture();await f.feature.controller.history();await tick();
    f.click(pdfTarget(7));const id=f.accept();await tick();
    f.click(rowTarget(9));await tick();
    assert.equal(f.hidden,1);assert.equal(f.feature.state.sessionId,9);assert.equal(f.feature.state.session.id,9);
    const readsAfterOpen=f.reads.length;
    f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult()});await tick();
    assert.equal(f.posts.length,1);assert.equal(f.posts[0].url,'/api/v2/daily-review/sessions/7/pdf');
    assert.equal(f.downloads.length,1);assert.equal(f.reads.length,readsAfterOpen);
    assert.equal(f.feature.state.sessionId,9);assert.equal(f.feature.state.session.id,9);scenarios+=1;
  }
  // Ordinary history opening still works and never submits an export.
  {
    const f=fixture();await f.feature.controller.history();await tick();
    const event=f.click(rowTarget(7));await tick();
    assert.equal(event.propagationStopped,false);assert.equal(f.hidden,1);
    assert.equal(f.feature.state.sessionId,7);assert.equal(f.reads.at(-1),'/api/v2/daily-review/sessions/7');
    assert.equal(f.posts.length,0);scenarios+=1;
  }
  // A queued answer, its undo window and an in-flight save are left alone.
  {
    const f=fixture();
    f.feature.controller.submit(2,'again');f.feature.controller.submit(1,'solo');
    const [,timer]=[...f.timers.entries()].at(-1);
    assert.equal(f.writes.length,1);
    const queued=f.feature.state.queued;
    const pending=f.feature.controller.reexportPdf(7,'two_column');
    assert.deepEqual(body(f.posts[0]),{layout:'two_column'});
    const id=f.accept();await tick();
    f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult()});await pending;
    assert.equal(f.feature.state.queued,queued);assert.equal(f.writes.length,1);
    assert.equal(f.feature.state.pendingIds.has(2),true);
    f.feature.controller.undo();assert.equal(f.feature.state.queued,null);assert.equal(f.writes.length,1);
    f.writes[0].resolve({completion_result:'again',assistance:'none'});await tick();
    assert.equal(f.feature.state.items.find(item=>item.id===2).result,'again');
    assert.equal(typeof timer,'function');scenarios+=1;
  }
  // Retained migration snapshots are disclosed, not presented as identical recovery.
  {
    const f=fixture();const pending=f.feature.controller.reexportPdf(7,'compact');
    const id=f.accept();await tick();f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult({legacy_live_content:true})});await pending;
    const [message,type,options]=f.toasts.at(-1);
    assert.ok(message.startsWith('PDF 已重新生成：'));assert.equal(type,'info');assert.match(message,/不完全一致/);
    assert.equal(options.persistent,true);
    assert.equal(f.downloads.length,1);scenarios+=1;
  }
  // Late failures and legacy notices must not replace an unrelated unsaved
  // editor. Use the production modal writer so an accidental showModal call
  // reproduces the destructive body replacement instead of merely recording it.
  for(const outcome of ['failure','legacy-success']){
    const f=fixture();
    f.context.modalRevision=0;
    f.context.MODAL_META={info:{type:'info',icon:'circleHelp'},error:{type:'error',icon:'x'}};
    f.context.requestAnimationFrame=callback=>callback();
    const modal=f.context.document.getElementById('modal');
    f.context.document.querySelector=selector=>selector==='#modal .modal-box'?modal:null;
    const core=fs.readFileSync(path.join(root,'core.js'),'utf8');
    vm.runInContext(core.slice(core.indexOf('function showModal('),core.indexOf('function sanitizeModalHTML(')),f.context);
    const body=f.context.document.getElementById('modal-body');
    let bodyText='';
    Object.defineProperty(body,'textContent',{get:()=>bodyText,set:value=>{bodyText=value;body.innerHTML='';}});
    const pending=f.feature.controller.reexportPdf(7,'compact');
    const id=f.accept();await tick();
    f.feature.controller.deactivate();
    const title=f.context.document.getElementById('modal-title');
    title.textContent='作答记录编辑';
    const draft='<textarea id="unsaved-answer">尚未保存的作答</textarea>';
    body.innerHTML=draft;
    const focused={value:'尚未保存的作答'};
    f.context.document.activeElement=focused;
    if(outcome==='failure')f.subscriptions[0].reject(jobError('failed','xelatex_failed','XeLaTeX failed'));
    else f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult({legacy_live_content:true})});
    await pending;
    assert.equal(body.innerHTML,draft,`${outcome} replaced the unsaved editor`);
    assert.equal(title.textContent,'作答记录编辑');
    assert.equal(f.context.modalRevision,0);
    assert.equal(f.context.document.activeElement,focused);
    assert.equal(f.toasts.length,1);assert.equal(f.toasts[0][2].persistent,true);
    assert.equal(f.downloads.length,outcome==='failure'?0:1);
    assert.equal(f.feature.state.reexports.size,0);scenarios+=1;
  }
  // The new-batch flow is unchanged and does not share the re-export identity.
  {
    const f=fixture();const exporting=f.feature.controller.reexportPdf(7,'compact');
    const generating=f.feature.controller.generate();await tick();
    assert.equal(f.jobs.length,1);assert.equal(f.jobs[0].kind,'mistake-export');
    assert.equal(f.jobs[0].input.export_context,'daily_review');assert.ok(f.jobs[0].input.daily_review_request_id);
    assert.equal(f.posts.length,1);
    f.jobs[0].resolve({item_count:1,download_url:'/mistake-export-batches/1.zip',download_name:'new.zip'});await generating;
    assert.equal(f.feature.state.reexports.has(7),true);
    const id=f.accept();await tick();f.subscriptions[0].resolve({id,status:'succeeded',result:jobResult()});await exporting;
    assert.deepEqual(f.downloads.map(item=>item.download),['new.zip','每日自主复习批次_42.zip']);scenarios+=1;
  }
  console.log(`DAILY_REVIEW_REEXPORT_CONTRACT_OK scenarios=${scenarios}`);
}

const watchdog=setTimeout(()=>{console.error('Daily review re-export contract timed out');process.exit(1)},5000);
run().then(()=>clearTimeout(watchdog),error=>{clearTimeout(watchdog);console.error(error);process.exitCode=1});
