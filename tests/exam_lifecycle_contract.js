'use strict';

const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  const nodes=new Map(),requests=[],jobs=[],modals=[];
  class Element extends EventTarget{
    constructor(tag='div'){
      super();this.tagName=tag;this.children=[];this.dataset={};this.style={};
      this.attributes={};this.value='';this.textContent='';this._html='';
      this.className='';this.hidden=false;this.disabled=false;this.isConnected=true;
      this.classList={
        contains:name=>this.className.split(' ').includes(name),
        add:name=>{if(!this.classList.contains(name))this.className+=' '+name},
        remove:name=>{this.className=this.className.split(' ').filter(x=>x!==name).join(' ')},
        toggle:(name,on)=>on?this.classList.add(name):this.classList.remove(name)
      };
    }
    set id(value){this._id=value;nodes.set(value,this)}
    get id(){return this._id}
    set innerHTML(html){
      this._html=html;this.children=[];
      if(html.includes('class="exam-selection-explanation-body"')){
        const child=new Element();child.className='exam-selection-explanation-body';this.append(child);
      }
    }
    get innerHTML(){return this._html}
    get options(){return this.children}
    append(...items){for(const item of items){item.parentElement=this;this.children.push(item)}}
    appendChild(item){this.append(item)}
    add(item){this.append(item)}
    replaceChildren(...items){this.children=[];this.append(...items)}
    setAttribute(name,value){this.attributes[name]=String(value)}
    getAttribute(name){return this.attributes[name]||null}
    removeAttribute(name){delete this.attributes[name]}
    remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(x=>x!==this);nodes.delete(this.id)}
    querySelectorAll(selector){
      const all=this.children.flatMap(child=>[child,...child.querySelectorAll('*')]);
      if(selector==='*')return all;
      return all.filter(child=>selector[0]==='.'?child.classList.contains(selector.slice(1)):selector[0]==='#'?child.id===selector.slice(1):child.tagName===selector);
    }
    querySelector(selector){return this.querySelectorAll(selector)[0]||null}
    insertAdjacentHTML(_position,html){
      this._html+=html;
      for(const match of html.matchAll(/id="([^"]+)"/g)){
        const child=new Element();child.id=match[1];this.append(child);
      }
    }
    insertAdjacentElement(_position,item){this.parentElement?.append(item)}
  }
  function element(id){const node=new Element();node.id=id;return node}
  for(const id of ['e-subject','e-chapters','e-diff','em-diff','e-diff-stars','em-diff-stars','em-subject','em-chapter','exam-bank-coverage','exam-daily-recommendations','exam-daily-recommendations-refresh','exam-history','exam-history-load-more','exam-history-total','exam-result','exam-content','exam-empty','modal-body','btn-gen','gen-status','gen-progress','e-target','e-count','e-note'])element(id);
  nodes.get('e-subject').replaceChildren(new Element('option'),new Element('option'));
  const tabs=new Element(),historyTabs=new Element();
  const doc={
    getElementById:id=>nodes.get(id)||null,
    createElement:tag=>new Element(tag),
    querySelector:selector=>selector==='.tabs'?tabs:selector==='.exam-history-tabs'?historyTabs:null,
    querySelectorAll:selector=>selector==='#e-types input:checked'?[{value:'证明题'}]:[],
    activeElement:null
  };
  function deferred(list,url,options){
    return new Promise((resolve,reject)=>list.push({url,options,resolve,reject}));
  }
  const context={console,AbortController,DOMException,URLSearchParams,Event,Set,Map,
    document:doc,Option:class extends Element{constructor(text,value){super('option');this.textContent=text;this.value=value}},
    safeInteger:(value,fallback=0)=>Number.isFinite(Number(value))?Number(value):fallback,
    escapeHTML:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
    icon:()=>'',syncCustomSelect:()=>{},renderMath:async()=>true,
    emptyStateHTML:text=>text,safeLocalURL:value=>value,
    api:(url,options={})=>deferred(requests,url,options),
    runJobRequest:(kind,payload)=>deferred(jobs,kind,payload),
    showModal:(title,message,_kind,buttons='')=>{
      modals.push({title,message});nodes.get('modal-body').replaceChildren();
      for(const match of buttons.matchAll(/id="([^"]+)"/g))element(match[1]);
    },hideModal:()=>{},showToast:()=>{}
  };
  const windowEvents=new EventTarget();
  context.window=context;context.addEventListener=windowEvents.addEventListener.bind(windowEvents);
  context.Cuoti={features:{register:(name,value)=>{assert.equal(name,'exam');context.contract=value}}};
  vm.createContext(context);
  const loader=fs.readFileSync(path.join(root,'cuoti_app/static/js/feature-loader.js'),'utf8');
  const urls=[...loader.match(/exam:Object.freeze\(\[([\s\S]*?)\]\)/)[1].matchAll(/'([^']+)'/g)].map(m=>m[1]);
  assert.equal(urls.length,8);
  assert.equal(urls.at(-1),'/static/js/exam.js?bundle=exam-modules-v1');
  const template=fs.readFileSync(path.join(root,'cuoti_app/templates/index.html'),'utf8');
  assert.ok(template.includes("filename='js/feature-loader.js', bundle='exam-modules-v1'"));
  const sw=fs.readFileSync(path.join(root,'cuoti_app/static/sw.js'),'utf8');
  for(const url of urls){
    const pathname=url.split('?')[0];
    assert.ok(sw.includes(`'${pathname}'`),`SW missing ${url}`);
    vm.runInContext(fs.readFileSync(path.join(root,'cuoti_app'+pathname),'utf8'),context,{filename:url});
  }
  return {context,page:context.Cuoti.exam,nodes,requests,jobs,modals};
}

async function run(){
  // Actual loader order executes all classic modules and preserves public actions.
  {
    const f=fixture();
    for(const action of ['mount','unmount','refresh','switchMode','selectAll','selectNone','generate'])assert.equal(typeof f.context.contract[action],'function');
    for(const action of ['loadExamChapters','loadPastExamSubjects','renderPastExamSource','toggleMistake'])assert.equal(typeof f.context[action],'function');
    assert.ok(f.page.preview.latexToHtml('<script>alert(1)</script>').includes('&lt;script&gt;'));
    f.page.selection.renderExamBankCoverage({errors:[{source_file:'fixture.tex',error:'白名单文件不存在 <source>'}]});
    assert.ok(f.nodes.get('exam-bank-coverage').innerHTML.includes('白名单文件不存在 &lt;source&gt;'));
    assert.ok(!f.nodes.get('exam-bank-coverage').innerHTML.includes('[object Object]'));
  }
  // Repeated mount binds one handler; remount replaces the aborted listeners.
  {
    const f=fixture();
    const first=f.context.contract.mount(),second=f.context.contract.mount();
    assert.equal(f.requests.length,2);
    f.requests[1].resolve({items:[],has_more:false});f.requests[0].resolve({items:[],has_more:false});
    await Promise.all([first,second]);
    f.nodes.get('exam-daily-recommendations-refresh').dispatchEvent(new Event('click'));
    assert.equal(f.requests.length,3);
    f.context.contract.unmount();
    assert.equal(f.requests[2].options.signal.aborted,true);
    f.requests[2].resolve({items:[]});await tick();
    const mounted=f.context.contract.mount();f.requests[3].resolve({items:[]});await mounted;
    f.nodes.get('exam-daily-recommendations-refresh').dispatchEvent(new Event('click'));
    assert.equal(f.requests.length,5);
    f.requests[4].resolve({items:[]});await tick();
  }
  // Slow active history cannot overwrite a newer trash page, even ignoring abort.
  {
    const f=fixture();f.page.state.active=true;
    const old=f.page.history.loadExamHistory();
    f.page.history.switchExamHistoryMode('trash');
    assert.equal(f.requests[0].options.signal.aborted,true);
    f.requests[1].resolve({items:[{id:2,title:'trash-new'}],total:2,has_more:true,next_cursor:'next'});await tick();
    f.requests[0].resolve({items:[{id:1,title:'active-old'}]});await old;
    assert.ok(f.nodes.get('exam-history').innerHTML.includes('trash-new'));
    assert.ok(!f.nodes.get('exam-history').innerHTML.includes('active-old'));
    const more=f.page.history.loadExamHistory({append:true});
    assert.ok(f.requests[2].url.includes('/trash/exams?limit=30&after=next'));
    f.requests[2].resolve({items:[{id:3,title:'trash-next'}],total:2});await more;
    assert.ok(f.nodes.get('exam-history').innerHTML.includes('trash-new'));
    assert.ok(f.nodes.get('exam-history').innerHTML.includes('trash-next'));
  }
  // Unmount invalidates a pending detail and pending math typesetting.
  {
    const f=fixture();f.page.state.active=true;
    const read=f.page.preview.viewExam(1);f.context.contract.unmount();
    f.requests[0].resolve({id:1,content:'obsolete',items:[]});await read;
    assert.equal(f.nodes.get('exam-content').innerHTML,'');
    assert.equal(f.page.state.currentExamPreviewId,null);
    assert.equal(f.modals.length,0);
    f.page.state.active=true;
    let finishMath;f.context.renderMath=()=>new Promise(resolve=>{finishMath=resolve});
    const preview=f.page.preview.viewExam(2);
    f.requests[1].resolve({id:2,content:'current',items:[]});await tick();
    f.context.contract.unmount();finishMath(true);await preview;
    assert.equal(f.page.state.currentExamPreviewId,null);
    assert.equal(f.nodes.has('exam-start-attempt'),false);
  }
  // Rapid subject changes keep the newer chapter/coverage pair authoritative.
  {
    const f=fixture();f.page.state.active=true;
    f.nodes.get('e-subject').value='1';const old=f.page.selection.loadExamChapters();
    f.nodes.get('e-subject').value='2';const current=f.page.selection.loadExamChapters();
    assert.equal(f.requests.length,4);
    f.requests[2].resolve([{id:22,name:'new-chapter'}]);
    f.requests[3].resolve({chapters:[{chapter_id:22,total_questions:3}],total_questions:3});await current;
    f.requests[0].resolve([{id:11,name:'old-chapter'}]);
    f.requests[1].resolve({chapters:[{chapter_id:11,total_questions:9}],total_questions:9});await old;
    assert.ok(f.nodes.get('e-chapters').innerHTML.includes('new-chapter'));
    assert.ok(!f.nodes.get('e-chapters').innerHTML.includes('old-chapter'));
    assert.equal(f.requests[0].options.signal.aborted,true);
    assert.equal(f.nodes.get('e-chapters').getAttribute('aria-busy'),null);
  }
  // Explanation reads are lazy, retryable, and aborted when preview is removed.
  {
    const f=fixture();f.page.state.active=true;f.page.state.currentExamPreviewId=9;
    f.page.preview.mountExamSelectionExplanation(9,f.nodes.get('exam-result'));
    assert.equal(f.requests.length,0);
    const details=f.nodes.get('exam-selection-explanation');details.open=true;
    details.dispatchEvent(new Event('toggle'));assert.equal(f.requests.length,1);
    f.requests[0].reject(new Error('temporary'));await tick();
    const body=details.querySelector('.exam-selection-explanation-body');
    const retry=body.querySelector('button');assert.ok(retry);
    retry.dispatchEvent(new Event('click'));assert.equal(f.requests.length,2);
    f.context.contract.unmount();assert.equal(f.requests[1].options.signal.aborted,true);
    f.requests[1].resolve({status:'available',items:[]});await tick();
    assert.equal(f.nodes.has('exam-selection-explanation'),false);
    assert.equal(f.modals.length,0);
  }
  // A submitted write completes server-side; its old-page UI continuation stops.
  {
    const f=fixture();f.page.state.active=true;
    const start=f.page.attempts.startExamAttempt(9);
    assert.equal(f.requests[0].options.method,'POST');
    assert.equal(f.requests[0].options.signal,undefined);
    f.context.contract.unmount();f.requests[0].resolve({public_id:'created',items:[]});await start;
    assert.equal(f.modals.length,0);
  }
  // New attempt history supersedes old history; editing preserves score/result fields.
  {
    const f=fixture();f.page.state.active=true;
    const first=f.page.attempts.showExamAttempts(1),second=f.page.attempts.showExamAttempts(2);
    f.requests[1].resolve({items:[]});await second;
    f.requests[0].resolve({items:[]});await first;assert.equal(f.modals.length,1);
    f.page.attempts.showExamAttemptEditor({public_id:'attempt',exam_id:2,status:'draft',items:[{public_id:'item',position:1,content:'question',result_status:'incorrect',assistance:'none',score:2,comment:'note'}]},2);
    const save=f.nodes.get('attempt-save').onclick();
    const saved=JSON.parse(f.requests[2].options.body);
    assert.deepEqual(saved.items,[{public_id:'item',result_status:'incorrect',assistance:'none',score:2,comment:'note'}]);
    assert.equal(saved.submit,false);assert.equal(f.requests[2].options.method,'PATCH');
    f.requests[2].resolve({public_id:'attempt',exam_id:2,status:'draft',items:[]});await save;
    assert.equal(f.modals.at(-1).title,'记录作答');
  }
  // The free generation payload and job lifetime survive navigation unchanged.
  {
    const f=fixture();f.page.state.active=true;
    f.nodes.get('e-subject').value='4';f.nodes.get('e-diff').value='中等';f.nodes.get('e-target').value='日常练习';f.nodes.get('e-count').value='5';
    f.page.state.selectedExamChapters.add(6);
    const generation=f.page.controller.generateExam();
    assert.equal(f.jobs[0].url,'exam-generation');
    assert.deepEqual(JSON.parse(JSON.stringify(f.jobs[0].options)),{source:'free',subject_id:4,chapters:[6],difficulty:'中等',target:'日常练习',question_count:5,types:['证明题'],note:''});
    f.context.contract.unmount();f.jobs[0].resolve({id:7,count:1});await generation;
    assert.equal(f.nodes.get('btn-gen').disabled,false);
    assert.ok(f.nodes.get('gen-status').innerHTML.includes('生成完成'));
  }
  // Return-to-mistakes keeps the explicit chapter/reason/comment payload.
  {
    const f=fixture();f.page.state.active=true;
    await f.page.attempts.openAttemptReturnDialog({public_id:'attempt',exam_id:2},{public_id:'item',comment:'keep'},2);
    f.nodes.get('attempt-return-chapter').value='6';f.nodes.get('attempt-return-copy-comment').checked=true;
    const query=f.nodes.get('modal-body').querySelectorAll.bind(f.nodes.get('modal-body'));
    f.nodes.get('modal-body').querySelectorAll=selector=>selector==='fieldset input:checked'?[{value:'概念不清'}]:query(selector);
    const returning=f.nodes.get('attempt-return-confirm').onclick();
    assert.equal(f.requests[0].url,'/api/v2/exam-attempt-items/item/return-mistake');
    assert.deepEqual(JSON.parse(f.requests[0].options.body),{chapter_id:6,reasons:['概念不清'],copy_comment:true});
    f.requests[0].resolve({ok:true});await tick();
    assert.equal(f.requests[1].url,'/api/v2/exam-attempts/attempt');
    f.requests[1].resolve({public_id:'attempt',status:'submitted',items:[]});await returning;
    assert.equal(f.modals.at(-1).title,'作答记录');
  }
  console.log('EXAM_LIFECYCLE_CONTRACT_OK groups=10');
}
run().catch(error=>{console.error(error);process.exitCode=1});
