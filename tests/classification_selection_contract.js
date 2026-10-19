'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function fixture(values){
  const calls=[],events=[],toasts=[];
  const nodes=new Map(['mistake-accept-suggestions','mistake-export-status','mistake-selected-count']
    .map(id=>[id,{textContent:'',disabled:false,innerHTML:''}]));
  const state={selectedPublicIds:new Set(Object.keys(values)),items:[],view:'current',exportInFlight:false};
  const context={
    document:{getElementById:id=>nodes.get(id)||null},icon:()=>'',
    Event:class{constructor(type){this.type=type}},
    showToast:(message,type)=>toasts.push({message,type}),
    api:async(url,options)=>{
      calls.push({url,body:options?JSON.parse(options.body):null});
      if(options){if(url.includes('/13/'))throw new Error('分类已更新');return {}}
      return values[decodeURIComponent(url.split('/').at(-2))];
    },
    window:{Cuoti:{mistakes:{state,api:{}}},dispatchEvent:event=>events.push(event.type)}
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../cuoti_app/static/js/mistakes/exports.js'),'utf8'),context);
  return {calls,events,toasts,nodes,state,feature:context.window.Cuoti.mistakes.exports};
}

const suggested=(id,changes={})=>({revision:4,mistake_revision:7,
  suggestion:{id,is_current:true,payload:{primary_type_code:'MA-1'},...changes}});

(async()=>{
  const f=fixture({valid:suggested(11),stale:suggested(12,{is_current:false}),
    conflict:suggested(13),newType:suggested(14,{payload:{proposed_type:{name:'新题型'}}}),
    none:{suggestion:null},last:suggested(15)});
  const work=f.feature.acceptSelectedSuggestions();
  assert.equal(f.nodes.get('mistake-accept-suggestions').disabled,true);
  // The operation keeps the selection made at click time and ignores later changes.
  f.state.selectedPublicIds.add('unselected-at-start');
  await f.feature.acceptSelectedSuggestions();
  await work;
  assert.deepEqual(f.calls.filter(call=>call.body).map(call=>call.url),[
    '/api/v2/classification-suggestions/11/resolve',
    '/api/v2/classification-suggestions/13/resolve',
    '/api/v2/classification-suggestions/15/resolve'
  ]);
  assert.deepEqual(f.calls[1].body,{action:'accept',revision:4,mistake_revision:7});
  assert.equal(f.calls.filter(call=>!call.body).length,6);
  assert.equal(f.nodes.get('mistake-accept-suggestions').disabled,false);
  assert.match(f.nodes.get('mistake-export-status').textContent,/已采纳 2 道；跳过 3 道.*失败 1 道：分类已更新/);
  assert.deepEqual(f.events,['cuoti:classification-changed']);

  const empty=fixture({});
  await empty.feature.acceptSelectedSuggestions();
  assert.equal(empty.calls.length,0);
  empty.feature.updateBar();
  assert.equal(empty.nodes.get('mistake-accept-suggestions').disabled,true);

  const skipped=fixture({old:suggested(1,{is_current:false})});
  await skipped.feature.acceptSelectedSuggestions();
  assert.equal(skipped.events.length,0);
  assert.equal(skipped.toasts[0].type,'warning');
  console.log('classification selection contract passed');
})().catch(error=>{console.error(error);process.exitCode=1});
