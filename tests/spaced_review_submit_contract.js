'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..','cuoti_app','static','js');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function fixture(){
  const requests=[],math=[],cleared=[],modals=[],nodes=new Map();
  let listWrites=0,dailyRenders=0,saveHandler=async()=>({ok:true}),readHandler;
  class Element{
    constructor(tag='div',id=''){this.tag=tag;this.id=id;this.children=[];this.dataset={};this.parentElement=null;this.html='';this.disabled=false;this.buttons=[]}
    get firstElementChild(){return this.children[0]||null}
    get nextElementSibling(){if(!this.parentElement)return null;const children=this.parentElement.children;return children[children.indexOf(this)+1]||null}
    get innerHTML(){return this.html}
    set innerHTML(html){
      this.html=html;
      for(const child of [...this.children])child.remove();
      if(this.id==='spaced-review-list'){
        listWrites++;
        if(!html.includes('review-table-scroll')){this.table=null;this.cards=null;return}
        this.table=new Element('tbody');this.cards=new Element('div');
        this.table.innerHTML=html.match(/<tbody>([\s\S]*?)<\/tbody>/)[1];
        this.cards.innerHTML=html.slice(html.indexOf('<div class="review-card-list">'));
        this.insertBefore(this.table,null);this.insertBefore(this.cards,null);
        return;
      }
      for(const match of html.matchAll(/<(tr|article)\b([^>]*)>([\s\S]*?)<\/(?:tr|article)>/g)){
        const node=new Element(match[1]);node.dataset.reviewId=match[2].match(/data-review-id="(\d+)"/)[1];node.html=match[0];
        node.buttons=[{disabled:match[0].includes(' disabled'),setAttribute(){}}];
        this.insertBefore(node,null);
      }
    }
    insertBefore(node,before){
      if(node===before)return;
      node.remove();const index=before?this.children.indexOf(before):this.children.length;
      assert.ok(index>=0);this.children.splice(index,0,node);node.parentElement=this;return node;
    }
    replaceChild(node,old){const index=this.children.indexOf(old);assert.ok(index>=0);node.remove();this.children[index]=node;node.parentElement=this;old.parentElement=null}
    remove(){if(this.parentElement){const p=this.parentElement;p.children.splice(p.children.indexOf(this),1);this.parentElement=null}}
    querySelector(selector){if(selector==='.review-table-scroll tbody')return this.table;if(selector==='.review-card-list')return this.cards;return null}
    querySelectorAll(selector){const id=selector.match(/data-id="(\d+)"/)?.[1];return [this.table,this.cards].filter(Boolean).flatMap(p=>p.children.filter(n=>n.dataset.reviewId===id).flatMap(n=>n.buttons))}
    setAttribute(){}
    addEventListener(){}
  }
  const element=id=>{if(!nodes.has(id))nodes.set(id,new Element('div',id));return nodes.get(id)};
  const context={console,AbortController,URLSearchParams,Map,Set,Promise,
    safeInteger:(v,f=0)=>Number.isSafeInteger(Number(v))?Number(v):f,
    escapeHTML:String,icon:()=>'',problemPreviewHTML:c=>`<div class="math">${c}</div>`,emptyStateHTML:()=>'<p>done</p>',
    crypto:{randomUUID:()=> 'event'},renderMath:async n=>math.push(n),showModal:(...args)=>modals.push(args),
    document:{getElementById:element,createElement:tag=>new Element(tag)},
    createCursorPager(limit){return {limit,total:0,nextCursor:null,hasMore:false,
      reset(){this.total=0;this.nextCursor=null;this.hasMore=false},params(){return new URLSearchParams({limit:String(limit)})},
      apply(page){this.total=page.total;this.hasMore=Boolean(page.has_more);this.nextCursor=page.next_cursor;return page.items}}},
    api:async(url,options={})=>{requests.push({url,options});if(options.method==='POST')return saveHandler(url,options);return readHandler(url,options)}};
  context.window={Cuoti:{daily:{render(){dailyRenders++}},reviewBadge:{refresh(){}},reviewEvidence:{value:()=> 'none',bind(){},control:()=>''}},MathJax:{typesetClear:items=>cleared.push(...items)}};
  vm.createContext(context);
  for(const name of ['review/state.js','review/render.js','review/api.js','review/controller.js'])vm.runInContext(fs.readFileSync(path.join(root,name),'utf8'),context,{filename:name});
  const review=context.window.Cuoti.review;
  const item=(id,revision=1)=>({id,public_id:`p${id}`,revision,subject:'s',chapter:'c',qnum:String(id),content:'math',note:'note'});
  let page={items:[item(1),item(2),item(3)],total:4,has_more:true,next_cursor:'next',summary:{active:4,mastered:0}};
  readHandler=async()=>page;
  return {context,review,requests,math,cleared,modals,nodes,item,
    setPage(value){page=value},save(fn){saveHandler=fn},read(fn){readHandler=fn},
    writes:()=>listWrites,daily:()=>dailyRenders,async load(){await review.controller.loadPage();math.length=0;cleared.length=0;requests.length=0}};
}

(async()=>{
  for(const result of ['pass','again']){
    const f=fixture();await f.load();const list=f.nodes.get('spaced-review-list');
    const kept=list.table.children[1];kept.noteExpanded=true;const card=list.cards.children[1];
    f.setPage({items:[f.item(2),f.item(3),f.item(4)],total:3,has_more:false,summary:{active:4,mastered:0}});
    await f.review.controller.submit(1,result);
    assert.equal(f.writes(),1);assert.equal(f.daily(),1);
    assert.equal(list.table.children[0],kept);assert.equal(kept.noteExpanded,true);assert.equal(list.cards.children[0],card);
    assert.equal(f.math.length,2);assert.ok(f.math.every(n=>n.dataset.reviewId==='4'));
    assert.equal(f.cleared.length,2);assert.equal(f.requests.length,2);
    assert.ok(f.requests.every(r=>!r.url.includes('/stats')));
  }
  {
    const f=fixture();await f.load();const before=f.nodes.get('spaced-review-list').table.children[0];
    f.save(async()=>{throw new Error('save failed')});await f.review.controller.submit(1,'pass');
    assert.equal(f.requests.length,1);assert.equal(f.writes(),1);assert.equal(f.math.length,0);
    assert.equal(f.nodes.get('spaced-review-list').table.children[0],before);assert.equal(before.buttons[0].disabled,false);assert.equal(f.modals.length,1);
  }
  {
    const f=fixture();await f.load();let resolveSave;
    f.save(()=>new Promise(resolve=>{resolveSave=resolve}));
    const first=f.review.controller.submit(1,'pass');await f.review.controller.submit(1,'again');
    assert.equal(f.requests.length,1);f.review.controller.deactivate();resolveSave({ok:true});await first;
    assert.equal(f.requests.length,1);assert.equal(f.writes(),1);assert.equal(f.review.state.pendingIds.size,0);
  }
  {
    const f=fixture();await f.load();const old=f.nodes.get('spaced-review-list').table.children[1];
    f.setPage({items:[f.item(2,2),f.item(3)],total:2,summary:{active:3,mastered:1}});
    await f.review.controller.submit(1,'pass');
    assert.notEqual(f.nodes.get('spaced-review-list').table.children[0],old);assert.equal(f.math.length,2);
  }
  {
    const f=fixture();await f.load();const reads=[];
    f.read(()=>new Promise(resolve=>reads.push(resolve)));
    const first=f.review.controller.submit(1,'pass');await tick();const second=f.review.controller.submit(2,'again');await tick();
    reads[1]({items:[f.item(3)],total:1,summary:{active:4,mastered:0}});await second;
    reads[0]({items:[f.item(2),f.item(3)],total:2,summary:{active:4,mastered:0}});await first;
    assert.deepEqual(Array.from(f.review.state.items,item=>item.id),[3]);assert.equal(f.writes(),1);assert.equal(f.math.length,0);
  }
  {
    const f=fixture();await f.load();f.setPage({items:[],total:0,summary:{active:0,mastered:4}});
    await f.review.controller.submit(1,'pass');assert.equal(f.nodes.get('spaced-review-list').innerHTML,'<p>done</p>');assert.equal(f.math.length,0);
  }
  {
    const f=fixture();await f.load();const original=f.review.state.items[0];let handlers;
    f.context.window.Cuoti.mistakes={editors:{showRevisionConflict(_error,value){handlers=value}}};
    f.save(async()=>{throw Object.assign(new Error('conflict'),{status:409,data:{code:'REVISION_CONFLICT'}})});
    await f.review.controller.submit(1,'pass');
    f.save(async(_url,options)=>{assert.equal(JSON.parse(options.body).revision,3);return {ok:true}});
    f.setPage({items:[f.item(2),f.item(3)],total:2,summary:{active:3,mastered:1}});
    await handlers.onRetry({currentRevision:3});
    assert.equal(original.revision,1);assert.equal(f.writes(),1);assert.equal(f.math.length,0);
  }
  console.log('SPACED_REVIEW_SUBMIT_CONTRACT_OK scenarios=8');
})().catch(error=>{console.error(error);process.exitCode=1});
