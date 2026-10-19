'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function fixture(){
  const shown=[],events=[];
  const body={className:'',innerHTML:'',querySelectorAll:()=>[]};
  const context={modalRevision:0,Event:class{constructor(type){this.type=type}},
    icon:name=>`<${name}>`,renderMath:async()=>{},safeInteger:Number,
    showModal(title){shown.push(title);context.modalRevision++},
    document:{querySelector:()=>({classList:{add(){}}}),getElementById:id=>id==='modal-body'?body:{addEventListener(){}}},
    window:{location:{hash:'#/list'},dispatchEvent:event=>events.push(event.type),
      Cuoti:{dailyAnalysis:{state:{requestRevision:0,pending:false},
        api:{fetchCache:async()=>({items:[],missing_count:0}),generate:async()=>{}},render:{body:()=>''}}}}
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../cuoti_app/static/js/daily-analysis/controller.js'),'utf8'),context);
  const trigger={isConnected:true,disabled:false,innerHTML:'<sparkle>',setAttribute(){},classList:{contains:()=>true}};
  return {context,feature:context.window.Cuoti.dailyAnalysis,trigger,shown,events};
}

(async()=>{
  const success=fixture();
  await success.feature.controller.open('2026-09-27','one',success.trigger);
  assert.equal(success.trigger.innerHTML,'<sparkle>');
  assert.equal(success.trigger.disabled,false);
  assert.deepEqual(success.shown,['单题每日分析']);
  const failure=fixture();
  failure.feature.api.fetchCache=async()=>{throw new Error('offline')};
  await failure.feature.controller.open('2026-09-27','one',failure.trigger);
  assert.equal(failure.trigger.innerHTML,'<sparkle>');
  assert.equal(failure.trigger.disabled,false);
  for(const change of ['route','modal','cancel']){
    const f=fixture();let resolve;
    f.feature.api.fetchCache=()=>new Promise(done=>{resolve=done});
    const work=f.feature.controller.open('2026-09-27','one',f.trigger);
    assert.equal(f.trigger.innerHTML,'<loader>');
    if(change==='route')f.context.window.location.hash='#/review';
    if(change==='modal')f.context.modalRevision++;
    if(change==='cancel')f.feature.controller.cancelPresentation();
    resolve({items:[],missing_count:0});await work;
    assert.equal(f.shown.length,0);
    assert.equal(f.trigger.innerHTML,'<sparkle>');
    assert.equal(f.feature.state.pending,false);
  }
  console.log('analysis interaction contract passed');
})().catch(error=>{console.error(error);process.exitCode=1});
