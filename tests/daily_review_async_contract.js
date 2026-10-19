'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..','cuoti_app','static','js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture({itemCount=1,holdReads=false,holdRemediation=false}={}){
  const nodes=new Map(),documentEvents={},windowEvents={},timers=new Map();
  const writes=[],reads=[],jobs=[],modals=[],remediations=[];
  let serial=0,modalOpen=false;
  const session={id:41,round_id:41,selection:{},items:Array.from({length:itemCount},(_,index)=>({
    id:index+1,round_id:41,subject_id:1,chapter_id:2,public_id:`p${index+1}`,
    completion_result:null,assistance:'unknown',content:'fixture'
  }))};
  const context={console,AbortController,URLSearchParams,Map,Set,Date,
    safeInteger:(v,f=0)=>Number.isSafeInteger(Number(v))?Number(v):f,
    escapeHTML:String,icon:()=>'',problemPreviewHTML:String,emptyStateHTML:String,
    syncCustomSelect:()=>{},renderMath:async()=>true,safeLocalURL:()=>'',
    showModal:(...args)=>modals.push(args),
    localStorage:{getItem:()=>null,setItem:()=>{}},crypto:{randomUUID:()=>String(++serial)},
    setTimeout:fn=>{const id=++serial;timers.set(id,fn);return id},
    clearTimeout:id=>timers.delete(id)
  };
  function element(id){
    if(nodes.has(id))return nodes.get(id);
    const node={id,hidden:id==='daily-drawer',dataset:{},style:{},value:'',options:[],isConnected:true,
      setAttribute:()=>{},removeAttribute:()=>{},getClientRects:()=>[{}],closest:()=>null,
      addEventListener:()=>{},querySelector:()=>null,
      querySelectorAll:()=>id==='daily-drawer'?[element('daily-drawer-close')]:[],
      contains:other=>other?.id==='daily-drawer-close',focus:()=>{context.document.activeElement=node},
      classList:{contains:name=>id==='p-review'?name==='on':id==='modal'?name==='hidden'&&!modalOpen:false,
        toggle:()=>{},add:()=>{},remove:()=>{}},
      appendChild:child=>{child.parentElement=node}
    };
    nodes.set(id,node);return node;
  }
  context.document={body:element('body'),getElementById:element,querySelector:()=>null,querySelectorAll:()=>[],
    addEventListener:(name,fn)=>{documentEvents[name]=fn},activeElement:null};
  context.window=context;context.Cuoti={};
  context.addEventListener=(name,fn)=>{windowEvents[name]=fn};
  context.api=(url,options={})=>{
    if(url.endsWith('/remediation')&&holdRemediation)return new Promise((resolve,reject)=>remediations.push({resolve,reject}));
    if(url.endsWith('/result'))return new Promise((resolve,reject)=>writes.push({url,options,resolve,reject}));
    if(url.includes('/current')){
      const payload=JSON.parse(JSON.stringify(session));
      if(holdReads)return new Promise(resolve=>reads.push({url,options,resolve:()=>resolve(payload)}));
      reads.push({url,options});return Promise.resolve(payload);
    }
    return Promise.resolve({items:[]});
  };
  context.runJobRequest=(kind,input)=>new Promise((resolve,reject)=>jobs.push({kind,input,resolve,reject}));
  vm.createContext(context);
  for(const name of ['state','api','render','controller']){
    vm.runInContext(fs.readFileSync(path.join(root,'daily-review',name+'.js'),'utf8'),context,{filename:name+'.js'});
  }
  const feature=context.Cuoti.dailyReview;
  feature.state.session=session;feature.state.items=session.items.map(feature.render.normalize);
  feature.controller.bind();
  return {feature,context,writes,reads,jobs,modals,element,timers,remediations,
    showModal:()=>{modalOpen=true},hideModal:()=>{modalOpen=false},
    pagehide:()=>windowEvents.pagehide(),keydown:event=>documentEvents.keydown(event),
    fireUndo(){const [id,fn]=[...timers.entries()].at(-1);timers.delete(id);fn()},
    commit(index=0,result='correct'){
      const write=writes[index];
      const id=Number(write.url.match(/items\/(\d+)/)[1]);
      const saved=JSON.parse(write.options.body);
      Object.assign(session.items.find(item=>item.id===id),{completion_result:result,assistance:saved.assistance});
      write.resolve({completion_result:result,assistance:saved.assistance});
    },session
  };
}

async function run(){
  // Hinted success adds a follow-up; independent cross-day success can remove it.
  for(const choice of ['hint','solo','incomplete']){
    const f=fixture({holdRemediation:true});
    f.feature.state.remediationItems=[{source_item_id:1}];
    f.feature.controller.submit(1,choice);f.fireUndo();f.commit(0,choice==='incomplete'?'incomplete':'correct');
    await tick();
    assert.equal(f.remediations.length,1);
    const items=choice==='solo'?[]:[{source_item_id:1}];
    f.remediations[0].resolve({items});await tick();
    assert.equal(f.feature.state.remediationItems.length,items.length);
  }
  // Concurrent saves share a refresh. A failed auxiliary read never undoes them.
  {
    const f=fixture({itemCount:2,holdRemediation:true});
    f.feature.controller.submit(1,'solo');f.fireUndo();
    f.feature.controller.submit(2,'hint');f.fireUndo();
    f.commit(0);await tick();assert.equal(f.remediations.length,0);
    f.commit(1);await tick();assert.equal(f.remediations.length,1);
    f.remediations[0].reject(new Error('offline'));await tick();
    assert.deepEqual(Array.from(f.feature.state.items,item=>item.result),['correct','correct']);
    assert.ok(f.element('daily-review-export-status').textContent.includes('作答已保存'));
  }
  // A response from before a session refresh cannot restore stale follow-ups.
  {
    const f=fixture({holdRemediation:true});
    f.feature.controller.submit(1,'solo');f.fireUndo();f.commit();await tick();
    const loaded=f.feature.controller.load();await tick();
    assert.equal(f.remediations.length,2);
    f.remediations[1].resolve({items:[]});await loaded;
    f.remediations[0].resolve({items:[{source_item_id:99}]});await tick();
    assert.equal(f.feature.state.remediationItems.length,0);
  }
  // Keep the visual interaction: no request during the undo window, undo cancels it.
  {
    const f=fixture();f.feature.controller.submit(1,'solo');
    assert.equal(f.writes.length,0);assert.equal(f.timers.size,1);
    f.feature.controller.undo();assert.equal(f.timers.size,0);f.pagehide();await tick();
    assert.equal(f.writes.length,0);assert.equal(f.feature.state.items[0].result,null);
  }
  // A timer-started write is awaited even though the queued card has been cleared.
  {
    const f=fixture();f.feature.controller.submit(1,'solo');f.fireUndo();
    const reload=f.feature.controller.load();await tick();
    assert.equal(f.reads.length,0);
    f.commit();await reload;
    assert.equal(f.reads.length,1);assert.equal(f.feature.state.items[0].result,'correct');
  }
  // A write beginning during an older read invalidates that read, even if abort is ignored.
  {
    const f=fixture({holdReads:true});const reload=f.feature.controller.load();await tick();
    f.feature.controller.submit(1,'solo');f.fireUndo();
    assert.equal(f.reads[0].options.signal.aborted,true);
    f.commit();await tick();f.reads[0].resolve();await reload;
    assert.equal(f.feature.state.items[0].result,'correct');
  }
  // Wait for a second card that starts saving while the first write is pending.
  {
    const f=fixture({itemCount:2});f.feature.controller.submit(1,'solo');f.fireUndo();
    const reload=f.feature.controller.load();await tick();
    f.feature.controller.submit(2,'hint');f.fireUndo();f.commit(0);await tick();
    assert.equal(f.reads.length,0);f.commit(1);await reload;
    assert.deepEqual(Array.from(f.feature.state.items,item=>item.result),['correct','correct']);
  }
  // Conflict recovery must not deadlock waiting on its own tracked write.
  {
    const f=fixture();f.feature.controller.submit(1,'solo');f.fireUndo();
    f.session.items[0].completion_result='again';
    f.writes[0].reject(Object.assign(new Error('already recorded'),{status:409,data:{code:'DAILY_REVIEW_CONFLICT'}}));
    await tick();await tick();
    assert.equal(f.feature.state.pendingIds.size,0);assert.equal(f.reads.length,1);
    assert.equal(f.feature.state.items[0].result,'again');assert.equal(f.modals[0][0],'结果已记录');
  }
  // An ordinary failure restores the pending card; leaving while waiting does not start a new read.
  {
    const f=fixture();f.feature.controller.submit(1,'solo');f.fireUndo();
    const reload=f.feature.controller.load();f.feature.controller.deactivate();
    f.writes[0].reject(new Error('offline'));await reload;await tick();
    assert.equal(f.feature.state.items[0].result,null);assert.equal(f.reads.length,0);
    assert.equal(f.modals[0][0],'记录每日复习结果失败');
  }
  // Double-click while a save is slow cannot enter the generation request twice.
  {
    const f=fixture();f.feature.controller.submit(1,'solo');
    const first=f.feature.controller.generate(),second=f.feature.controller.generate();
    await second;await tick();assert.equal(f.feature.state.generationInFlight,true);
    assert.equal(f.jobs.length,0);f.commit();await tick();
    assert.equal(f.jobs.length,1);assert.equal(f.jobs[0].kind,'mistake-export');
    f.jobs[0].resolve({item_count:1});await first;
    assert.equal(f.jobs.length,1);assert.equal(f.feature.state.generationInFlight,false);
  }
  // Method focus reaches the existing export job without accidentally adding a type filter.
  {
    const f=fixture();f.feature.state.practiceMode='method';
    f.element('daily-focus-subject').value='1';
    f.element('daily-focus-method').value='integration_by_parts';
    f.element('daily-focus-method-role').value='any';
    f.element('daily-focus-count').value='5';
    f.element('daily-focus-mastered').checked=false;
    const work=f.feature.controller.generate();await tick();
    const selection=f.jobs[0].input.selection;
    assert.equal(selection.practice_mode,'focused');
    assert.equal(selection.method_code,'integration_by_parts');
    assert.equal(selection.method_role,'any');
    assert.equal(selection.subject_id,'1');
    assert.equal(Object.hasOwn(selection,'primary_type_code'),false);
    f.jobs[0].resolve({item_count:1});await work;
  }
  // Generation failure still releases the lock and preserves the existing retry identity.
  {
    const f=fixture();const first=f.feature.controller.generate();await tick();
    f.jobs[0].reject(new Error('failed'));await first;
    assert.equal(f.feature.state.generationInFlight,false);
    const retry=f.feature.controller.generate();await tick();assert.equal(f.jobs.length,2);
    assert.equal(f.jobs[0].input.daily_review_request_id,f.jobs[1].input.daily_review_request_id);
    f.jobs[1].resolve({item_count:1});await retry;
  }
  // The top modal keeps Tab/Escape; the drawer still traps focus when no modal is open.
  {
    const f=fixture();f.element('daily-drawer').hidden=false;f.showModal();
    const cancel=f.element('modal-cancel');f.context.document.activeElement=cancel;
    for(const key of ['Tab','Escape']){
      let prevented=false;
      f.keydown({key,target:cancel,defaultPrevented:false,preventDefault:()=>{prevented=true}});
      assert.equal(prevented,false);assert.equal(f.context.document.activeElement,cancel);
    }
    f.hideModal();let prevented=false;
    f.keydown({key:'Tab',target:cancel,defaultPrevented:false,preventDefault:()=>{prevented=true}});
    assert.equal(prevented,true);assert.equal(f.context.document.activeElement.id,'daily-drawer-close');
  }
  // pagehide sends the queued result immediately with a small keepalive body.
  // Already-started writes also have keepalive and are not duplicated on departure.
  for(const alreadyStarted of [false,true]){
    const f=fixture();f.feature.controller.submit(1,'hint');if(alreadyStarted)f.fireUndo();
    f.pagehide();assert.equal(f.writes.length,1);
    const options=f.writes[0].options,body=JSON.parse(options.body);
    assert.equal(options.keepalive,true);assert.equal(options.method,'POST');
    assert.deepEqual(body,{result:'correct',assistance:'hint',idempotency_key:options.headers['Idempotency-Key']});
    assert.ok(Buffer.byteLength(options.body)<65536);
    f.commit();await tick();assert.equal(f.session.items[0].completion_result,'correct');
  }
  // Keyboard/controller entry cannot submit a detached historical item.
  {
    const f=fixture();f.feature.state.items[0].readOnly=true;
    f.feature.controller.submit(1,'solo');f.pagehide();
    assert.equal(f.writes.length,0);assert.equal(f.feature.state.queued,null);
  }
  console.log('DAILY_REVIEW_ASYNC_CONTRACT_OK scenarios=18');
}

const watchdog=setTimeout(()=>{console.error('Daily review async contract timed out');process.exit(1)},5000);
run().then(()=>clearTimeout(watchdog),error=>{clearTimeout(watchdog);console.error(error);process.exitCode=1});
