'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  const nodes=new Map(),reads=[],writes=[],radios=new Map();
  function radio(name,value){
    if(!radios.has(name))radios.set(name,[]);
    const group=radios.get(name);
    for(const node of group)node.checked=false;
    let node=group.find(item=>item.value===value);
    if(!node){node={name,value};group.push(node)}node.checked=true;
  }
  for(const [name,values] of Object.entries({'daily-summary-dimension':['method','type'],'daily-summary-scope':['30days','batch'],'daily-practice-mode':['method','focused','mixed']}))for(const value of values)radio(name,value);
  function el(id){
    if(nodes.has(id))return nodes.get(id);
    const node={id,hidden:id==='daily-drawer',dataset:{},style:{},value:'',options:[],handlers:{},isConnected:true,
      addEventListener(name,fn){this.handlers[name]=fn},setAttribute(){},removeAttribute(){},getClientRects:()=>[{}],
      classList:{toggle(){},add(){},remove(){},contains:()=>false},closest:()=>null,
      replaceChildren(...values){this.options=values;this.value=values[0]?.value||''},add(value){this.options.push(value)},
      querySelector:()=>null,querySelectorAll:()=>[],appendChild(node){node.parentElement=this},focus(){context.document.activeElement=this}};
    nodes.set(id,node);return node;
  }
  const context={console,AbortController,URLSearchParams,Map,Set,Date,
    safeInteger:(value,fallback=0)=>Number.isSafeInteger(Number(value))?Number(value):fallback,
    escapeHTML:String,icon:()=>'',problemPreviewHTML:String,emptyStateHTML:String,safeLocalURL:()=>'',
    Option:function(text,value){this.text=text;this.value=String(value)},syncCustomSelect(){},renderMath:async()=>true,
    setTimeout,clearTimeout,localStorage:{getItem:()=>null,setItem(){}},showModal(){},crypto:{randomUUID:()=> 'new-key'},
    runJobRequest:(...args)=>writes.push(args),
    document:{getElementById:el,querySelector:selector=>{
      const name=selector.match(/\[name="([^"]+)"\]:checked/);return name?radios.get(name[1])?.find(node=>node.checked):null;
    },querySelectorAll:selector=>{const name=selector.match(/^\[name="([^"]+)"\]$/);return name?radios.get(name[1])||[]:[]},addEventListener(){}},
    api:(url,options={})=>{
      if(url.includes('-summary?'))return new Promise((resolve,reject)=>reads.push({url,options,resolve,reject}));
      if(url==='/api/subjects')return Promise.resolve([{id:1,name:'数学分析'}]);
      if(url.startsWith('/api/chapters'))return Promise.resolve([{id:2,name:'数列极限'}]);
      if(url==='/api/v2/solution-methods')return Promise.resolve({items:[{code:'subsequence_method',name:'子列法',subjects:['数学分析'],definition:'合成说明'}]});
      if(url.includes('/current'))return Promise.resolve({id:3,items:[],selection:{}});
      return Promise.resolve({items:[]});
    }};
  context.window=context;context.addEventListener=()=>{};context.Cuoti={};context.document.body=el('body');
  vm.createContext(context);
  for(const name of ['state','api','render','controller'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../cuoti_app/static/js/daily-review',name+'.js'),'utf8'),context);
  const feature=context.Cuoti.dailyReview;
  const renderer=feature.render;
  feature.render={...feature.render,methodSummary:result=>result.marker,summaryTable:items=>items[0]?.marker||''};
  return {context,feature,renderer,el,reads,writes,radio,change:target=>el('daily-drawer').handlers.change({target})};
}

async function run(){
  {
    const f=fixture();
    const row={method_name:'子列法',method_code:'subsequence_method',subject_id:1,subject:'数学分析',practice_mode:'focused',independent_sample_count:4,independent_accuracy:null,unique_mistake_count:5,assisted_count:1,remediation_count:1,recovered_count:0};
    const small=f.renderer.methodSummary({items:[row],unattributed_count:2});
    assert.match(small,/样本不足/);assert.doesNotMatch(small,/daily-method-bar|%/);assert.match(small,/2<\/b> 条练习记录/);
    const full=f.renderer.methodSummary({items:[{...row,independent_sample_count:5,independent_accuracy:.8}]});
    assert.match(full,/80<small>%/);assert.match(full,/data-practice-method="subsequence_method"/);
  }
  {
    const f=fixture();await f.feature.controller.mount();f.feature.controller.openDrawer('summary');
    f.radio('daily-summary-dimension','method');f.change({name:'daily-summary-dimension'});
    assert.match(f.reads[1].url,/method-summary\?session_id=3/);
    assert.equal(f.reads[0].options.signal.aborted,true);
    f.reads[1].resolve({marker:'方法最新范围'});await tick();
    f.reads[0].resolve({items:[{marker:'过期题型范围'}]});await tick();
    assert.equal(f.el('daily-type-summary-content').innerHTML,'方法最新范围');
    f.el('daily-summary-subject').value='1';f.change({id:'daily-summary-subject'});await tick();
    assert.match(f.reads[2].url,/subject_id=1/);
    f.el('daily-summary-chapter').value='2';f.change({id:'daily-summary-chapter'});
    assert.match(f.reads[3].url,/subject_id=1&chapter_id=2/);
    f.feature.controller.closeDrawer();f.reads[3].reject(new Error('late error'));f.reads[2].resolve({marker:'旧章节'});await tick();
    assert.doesNotMatch(f.el('daily-type-summary-content').textContent||'',/late error/);
    assert.notEqual(f.el('daily-type-summary-content').innerHTML,'旧章节');
  }
  // A shared link fills subject/method/primary role and opens the existing form without creating a batch.
  {
    const f=fixture();await f.feature.controller.mount(new URLSearchParams('subject_id=1&method_code=subsequence_method'));
    assert.equal(f.feature.state.drawer,'generate');assert.equal(f.feature.state.practiceMode,'method');
    assert.equal(f.el('daily-focus-subject').value,'1');assert.equal(f.el('daily-focus-method').value,'subsequence_method');
    assert.equal(f.el('daily-focus-method-role').value,'primary');assert.equal(f.writes.length,0);
  }
  console.log('METHOD_SUMMARY_CONTRACT_OK');
}
run().catch(error=>{console.error(error);process.exitCode=1});
