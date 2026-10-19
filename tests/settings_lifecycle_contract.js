'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture({oldCache=false}={}){
  const nodes=new Map(),requests=[],timers=new Map();let timerId=0;
  class Element extends EventTarget{
    constructor(){super();this.value='';this.textContent='';this.children=[];this.dataset={};this.attributes={};this.style={};this.classList={toggle(){},contains:()=>false};}
    setAttribute(name,value){this.attributes[name]=value;}
    getAttribute(name){return this.attributes[name]||null;}
    removeAttribute(name){delete this.attributes[name];}
    append(...items){this.children.push(...items);}
    appendChild(item){this.children.push(item);}
    replaceChildren(...items){this.children=items;}
    querySelectorAll(){return [];}
    focus(){}
  }
  const absent=new Set(['ai-usage-stats','ai-memory-list','ai-memory-profile-list']);
  const element=id=>{if(absent.has(id))return null;if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const lanes=new Map(['fast','quality'].map(tier=>[`[data-lane-body="${tier}"]`,new Element()]));
  const laneOf=chip=>[...lanes].find(([,lane])=>lane.children.includes(chip))?.[0].match(/"(\w+)"/)[1];
  const chips=()=>[...lanes.values()].flatMap(lane=>lane.children).filter(node=>node.dataset?.task);
  const events=new EventTarget();
  const context={console,URL,URLSearchParams,AbortController,DOMException,Event,
    document:{getElementById:element,querySelectorAll:()=>[],querySelector:selector=>lanes.get(selector)||null,addEventListener(){},createElement:()=>new Element(),hidden:false},
    api:(url,options={})=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
    addEventListener:events.addEventListener.bind(events),
    setTimeout:callback=>{timers.set(++timerId,callback);return timerId;},clearTimeout:id=>timers.delete(id),
    safeInteger:(value,fallback=0)=>Number.isFinite(Number(value))?Number(value):fallback,
    escapeHTML:String,icon:()=>'',showModal(){},showRichModal(){},hideModal(){},
    Cuoti:{features:{register:(_name,contract)=>{context.contract=contract;}},ui:{}}};
  context.window=context;vm.createContext(context);
  const loader=fs.readFileSync(path.join(root,'cuoti_app/static/js/feature-loader.js'),'utf8');
  const bundle=loader.match(/settings:Object.freeze\(\[([\s\S]*?)\]\)/)[1];
  for(const match of bundle.matchAll(/'([^']+\.js(?:\?[^']*)?)'/g)){
    const relative=match[1].split('?')[0].replace('/static/','');
    const cachedState="window.Cuoti.settings={state:Object.freeze({dirtyFields:new Set()})};";
    const source=oldCache&&match[1]==='/static/js/settings/state.js'?cachedState:fs.readFileSync(path.join(root,'cuoti_app/static',relative),'utf8');
    vm.runInContext(source,context,{filename:relative});
  }
  function flushAuxiliary(){
    for(const r of requests){
      if(r.done||r.url==='/api/v2/settings/ai'||r.url==='/api/v2/question-bank/status')continue;
      r.done=true;r.resolve(r.url.includes('/health')?{contract_version:'1',checks:[],overall_status:'healthy'}:{schema:'semantic-index-operations-v1',settings:{},settings_state:{},job_state:{},counts:{},items:[]});
    }
  }
  const pending=(url,method='GET')=>requests.filter(r=>r.url===url&&(r.options.method||'GET')===method&&!r.done);
  const resolve=(r,payload)=>{r.done=true;r.resolve(payload);};
  return {context,requests,element,timers,pending,resolve,flushAuxiliary,chips,laneOf};
}

async function ready(f){
  const mounted=f.context.contract.mount();
  f.resolve(f.pending('/api/v2/settings/ai')[0],{version:1,base_url:'https://example.invalid',quality_model:'initial',fast_model:'fast'});
  f.resolve(f.pending('/api/v2/question-bank/status')[0],{available:true,stale:false});
  f.flushAuxiliary();await mounted;await tick();
}

(async()=>{
  {
    const f=fixture();
    const first=f.context.contract.mount();
    f.context.contract.unmount();
    const second=f.context.contract.mount();
    const reads=f.pending('/api/v2/settings/ai');
    f.resolve(reads[1],{version:2,quality_model:'new-model'});
    for(const r of f.pending('/api/v2/question-bank/status'))f.resolve(r,{available:true});
    f.flushAuxiliary();await second;
    f.element('ai-quality-model').value='unsaved-user-model';
    f.element('ai-quality-model').dispatchEvent(new Event('input'));
    f.resolve(reads[0],{version:1,quality_model:'old-model'});
    f.flushAuxiliary();await first;
    assert.equal(f.element('ai-quality-model').value,'unsaved-user-model');
    assert.equal(f.context.Cuoti.settings.state.dirtyFields.has('quality_model'),true);
  }
  {
    const f=fixture();await ready(f);
    const older=f.context.loadQuestionBankStatus();const newer=f.context.loadQuestionBankStatus();
    const reads=f.pending('/api/v2/question-bank/status');
    f.resolve(reads[1],{available:true,stale:false});await newer;
    const label=f.element('question-bank-state').textContent;
    f.resolve(reads[0],{available:true,stale:true});await older;
    assert.equal(f.element('question-bank-state').textContent,label);
    const pending=f.context.loadQuestionBankStatus();f.context.contract.unmount();
    f.element('question-bank-state').textContent='left-page';
    f.resolve(f.pending('/api/v2/question-bank/status')[0],{available:true,rebuilding:true});await pending;
    assert.equal(f.element('question-bank-state').textContent,'left-page');
    assert.equal(f.timers.size,0);
  }
  {
    const f=fixture();await ready(f);
    f.element('ai-quality-model').value='submitted';f.element('ai-quality-model').dispatchEvent(new Event('input'));
    const save=f.context.Cuoti.settings.connection.saveAISettings({preventDefault(){}});
    f.element('ai-quality-model').value='new-draft';f.element('ai-quality-model').dispatchEvent(new Event('input'));
    f.resolve(f.pending('/api/v2/settings/ai','PUT')[0],{version:2,base_url:'https://example.invalid',quality_model:'submitted'});
    await save;
    assert.equal(f.element('ai-quality-model').value,'new-draft');
    assert.equal(f.context.Cuoti.settings.state.dirtyFields.has('quality_model'),true);
    f.flushAuxiliary();
  }
  {
    const f=fixture();await ready(f);
    const old=f.context.loadQuestionBankStatus(),newer=f.context.loadQuestionBankStatus();
    const reads=f.pending('/api/v2/question-bank/status');
    reads[0].done=true;reads[0].reject(new Error('old request failed'));await old;
    assert.equal(f.element('question-bank-refresh').disabled,true);
    f.resolve(reads[1],{available:true});await newer;
    assert.equal(f.element('question-bank-refresh').disabled,false);
  }
  {
    const f=fixture();await ready(f);
    f.element('ai-quality-model').value='submitted';f.element('ai-quality-model').dispatchEvent(new Event('input'));
    const save=f.context.Cuoti.settings.connection.saveAISettings({preventDefault(){}});
    const write=f.pending('/api/v2/settings/ai','PUT')[0];assert.equal(write.options.signal,undefined);
    f.context.contract.unmount();
    const remount=f.context.contract.mount();
    f.resolve(f.pending('/api/v2/question-bank/status')[0],{available:true});f.flushAuxiliary();await remount;
    assert.equal(f.element('ai-save').disabled,true);
    f.element('ai-settings-status').textContent='new page';
    f.resolve(write,{version:2,quality_model:'submitted'});await save;
    assert.equal(f.element('ai-save').disabled,false);
    assert.equal(f.element('ai-settings-status').textContent,'new page');
  }
  {
    const f=fixture({oldCache:true});await ready(f);
    assert.equal(f.element('ai-quality-model').value,'initial');
    assert.equal(f.context.Cuoti.settings.state.active,true);
  }
  {
    // 模型分工点一下就保存：只提交分工本身；连续点击排成一个队列，读回设置后仍保留还没保存的换道
    const f=fixture();await ready(f);
    const chip=key=>f.chips().find(node=>node.dataset.task===key);
    assert.deepEqual(f.chips().map(node=>[node.dataset.task,f.laneOf(node)]),
      [['classification','fast'],['problem_analysis','quality'],['daily_analysis','quality'],['exam_generation','quality']]);
    chip('daily_analysis').dispatchEvent(new Event('click'));
    const first=f.pending('/api/v2/settings/ai','PUT');
    assert.equal(first.length,1);
    const body=JSON.parse(first[0].options.body);
    assert.deepEqual(Object.keys(body).sort(),['active_profile','api_key_action','assignments','version']);
    assert.equal(body.api_key_action,'keep');
    assert.equal(body.version,1);
    assert.deepEqual(body.assignments,{classification:'fast',problem_analysis:'quality',daily_analysis:'fast',exam_generation:'quality'});
    assert.equal(f.laneOf(chip('daily_analysis')),'fast');
    chip('exam_generation').dispatchEvent(new Event('click'));
    assert.equal(f.pending('/api/v2/settings/ai','PUT').length,1);
    f.resolve(first[0],{version:2,base_url:'https://example.invalid',quality_model:'initial',fast_model:'fast',assignments:{daily_analysis:'fast'}});
    await tick();await tick();
    assert.equal(f.laneOf(chip('exam_generation')),'fast');
    const second=f.pending('/api/v2/settings/ai','PUT');
    assert.equal(second.length,1);
    const next=JSON.parse(second[0].options.body);
    assert.equal(next.version,2);
    assert.deepEqual(next.assignments,{classification:'fast',problem_analysis:'quality',daily_analysis:'fast',exam_generation:'fast'});
    f.resolve(second[0],{version:3,base_url:'https://example.invalid',quality_model:'initial',fast_model:'fast',assignments:{daily_analysis:'fast',exam_generation:'fast'}});
    await tick();await tick();
    assert.equal(f.pending('/api/v2/settings/ai','PUT').length,0);
    assert.equal(f.laneOf(chip('exam_generation')),'fast');
    f.context.contract.unmount();
    assert.equal(f.timers.size,0);
  }
  console.log('SETTINGS_LIFECYCLE_CONTRACT_OK groups=7');
})().catch(error=>{console.error(error);process.exitCode=1;});
