'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  const nodes=new Map(),requests=[],events={};let serial=0;
  const element=id=>{
    if(nodes.has(id))return nodes.get(id);
    const node={id,value:'',hidden:true,disabled:false,innerHTML:'',textContent:'',dataset:{},options:[],handlers:{},isConnected:true,
      addEventListener(name,fn){this.handlers[name]=fn},replaceChildren(...values){this.options=values},appendChild(node){node.parentElement=this},contains:()=>false,
      querySelectorAll:()=>[],querySelector:()=>null,setAttribute(){},removeAttribute(){},getClientRects:()=>[{}],
      reportValidity:()=>true,focus(){context.document.activeElement=this}};
    nodes.set(id,node);return node;
  };
  const context={console,AbortController,URLSearchParams,Map,Set,DOMException,
    escapeHTML:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
    safeInteger:value=>Number(value)||0,syncCustomSelect:()=>{},icon:()=>'',emptyStateHTML:String,crypto:{randomUUID:()=>`request-${++serial}`},
    Option:function(text,value){this.text=text;this.value=value},
    document:{getElementById:element,querySelectorAll:()=>[],addEventListener:(name,fn)=>{events[name]=fn}},
    api:(url,options={})=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  context.window=context;context.Cuoti={exam:{state:{active:true,revision:0}}};
  context.document.body=element('body');
  vm.createContext(context);
  for(const name of ['api','issues'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../cuoti_app/static/js/exam',name+'.js'),'utf8'),context);
  const issues=context.Cuoti.exam.issues;
  issues.bind();issues.setPreview({id:7,title:'合成试卷',items:[{public_id:'item-1',position:1,content:'x < 3',question_type:'计算题'}]});
  element('exam-issues-scope').value='current';element('exam-issues-status').value='';
  const emit=(id,name,event={})=>element(id).handlers[name]({preventDefault(){},...event});
  function action(id,kind){return emit('exam-issue-list','click',{target:{closest:selector=>selector==='[data-issue-id]'?{dataset:{issueId:id}}:selector===`[data-issue-${kind}]`?{}:null}})}
  function input(id,kind,value){return emit('exam-issue-list','input',{target:{value,closest:()=>({dataset:{issueId:id}}),matches:selector=>selector===`[data-issue-${kind}]`}})}
  return {context,issues,requests,element,emit,action,input,events};
}
const row={public_id:'issue-1',revision:1,status:'open',category:'formula',created_at:'2026-09-27T12:00:00Z',description:'待核对 <script>',handling_note:'原备注',snapshot:{position:1,exam_title:'合成试卷',content:'x < 3'}};
const page=items=>({items,total:items.length,next_cursor:null});

async function run(){
  // Transport failures preserve the draft and replay key. A slow write cannot be submitted twice.
  {
    const f=fixture();f.issues.open('report');
    f.element('exam-issue-question').value='item-1';f.element('exam-issue-category').value='formula';
    f.element('exam-issue-description').value='公式缺失';f.emit('exam-issue-form','input');
    const first=f.emit('exam-issue-form','submit');f.emit('exam-issue-form','submit');
    assert.equal(f.requests.length,1);assert.equal(f.element('exam-issue-submit').disabled,true);
    f.requests[0].reject(new Error('offline'));await first;
    assert.equal(f.element('exam-issue-description').value,'公式缺失');
    f.issues.close();f.issues.open('report');
    assert.equal(f.element('exam-issue-description').value,'公式缺失');
    const retry=f.emit('exam-issue-form','submit');
    assert.equal(JSON.parse(f.requests[0].options.body).client_event_id,JSON.parse(f.requests[1].options.body).client_event_id);
    f.requests[1].resolve({...row,replayed:true});await retry;
    f.requests[2].resolve(page([row]));await tick();
    assert.match(f.element('exam-issue-list').innerHTML,/&lt;script&gt;/);
    f.issues.open('report');assert.equal(f.element('exam-issue-description').value,'');
  }
  // A previous filter response cannot paint the latest filter, even if the transport ignores abort.
  {
    const f=fixture();f.issues.open('list');
    f.element('exam-issues-status').value='resolved';f.emit('exam-issues-status','change');
    f.requests[1].resolve(page([{...row,description:'最新范围'}]));await tick();
    f.requests[0].resolve(page([{...row,description:'过期范围'}]));await tick();
    assert.match(f.element('exam-issue-list').innerHTML,/最新范围/);
    assert.doesNotMatch(f.element('exam-issue-list').innerHTML,/过期范围/);
  }
  // Revision conflict keeps the local note; adopting the latest revision never auto-saves it.
  {
    const f=fixture();f.issues.open('list');f.requests[0].resolve(page([{...row}]));await tick();
    f.input('issue-1','note','本地草稿 <内容>');f.input('issue-1','status','resolved');f.action('issue-1','save');
    f.requests[1].reject(Object.assign(new Error('conflict'),{data:{code:'REVISION_CONFLICT',current:{...row,revision:2,status:'dismissed',handling_note:'另一页面'}}}));await tick();
    assert.match(f.element('exam-issue-list').innerHTML,/本地草稿 &lt;内容&gt;/);
    assert.match(f.element('exam-issue-list').innerHTML,/另一页面/);
    f.action('issue-1','latest');assert.equal(f.requests.length,2);
    f.action('issue-1','save');
    assert.deepEqual(JSON.parse(f.requests[2].options.body),{revision:2,status:'resolved',handling_note:'本地草稿 <内容>'});
    f.requests[2].resolve({...row,revision:3,status:'resolved',handling_note:'本地草稿 <内容>'});await tick();
    assert.doesNotMatch(f.element('exam-issue-list').innerHTML,/data-issue-latest/);
    f.action('issue-1','reopen');assert.equal(JSON.parse(f.requests[3].options.body).status,'open');
    f.requests[3].resolve({...row,revision:4});await tick();
  }
  // Closing returns focus and keeps the draft. A completed old-page write has no UI continuation.
  {
    const f=fixture(),trigger=f.element('exam-issue-report-open');f.context.document.activeElement=trigger;f.issues.open('report');
    f.element('exam-issue-question').value='item-1';f.element('exam-issue-category').value='formula';
    f.element('exam-issue-description').value='保留草稿';f.emit('exam-issue-form','input');
    f.events.keydown({key:'Escape',preventDefault(){}});assert.equal(f.context.document.activeElement,trigger);
    f.issues.open('report');const write=f.emit('exam-issue-form','submit');
    f.context.Cuoti.exam.state.revision++;f.issues.unbind();f.requests[0].resolve(row);await write;
    assert.equal(f.element('exam-issues-drawer').hidden,true);assert.equal(f.requests.length,1);
  }
  // Reports remain reachable without any active exam preview (including an all-trash history).
  {
    const f=fixture();f.issues.setPreview(null);f.emit('exam-issues-history-open','click');
    assert.equal(f.element('exam-issues-scope').value,'all');
    assert.doesNotMatch(f.requests[0].url,/exam_id=/);
    f.requests[0].resolve(page([{...row,exam_deleted:true}]));await tick();
    assert.match(f.element('exam-issue-list').innerHTML,/试卷已移入回收站/);
  }
  console.log('EXAM_ISSUES_CONTRACT_OK');
}
run().catch(error=>{console.error(error);process.exitCode=1});
