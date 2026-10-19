'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const panels=new Map();
  class Element extends EventTarget{
    constructor(){super();this.dataset={};this.children=[];this.attributes={};this.on=false;this.classList={contains:()=>this.on};}
    setAttribute(key,value){this.attributes[key]=value;}
    removeAttribute(key){delete this.attributes[key];}
    toggleAttribute(key,on){if(on)this.setAttribute(key,'');else this.removeAttribute(key);}
    querySelector(){return this.children[0]||null;}
    prepend(child){this.children.unshift(child);}
    replaceChildren(...children){this.children=children;}
    remove(){}
  }
  for(const route of ['settings','exam','stats'])panels.set('p-'+route,new Element());
  const ctx={console:{error(){},warn(){}},performance,Event,
    CustomEvent:class extends Event{constructor(name,options){super(name);this.detail=options?.detail;}},
    setTimeout,clearTimeout,dispatchEvent(){},
    document:{getElementById:id=>panels.get(id),createElement:()=>new Element(),createTextNode:text=>({textContent:text}),
      head:{appendChild:node=>queueMicrotask(()=>node.dispatchEvent(new Event('load')))}}};
  ctx.window=ctx;vm.createContext(ctx);
  const file=process.env.CUOTI_LOADER_BASELINE||path.resolve(__dirname,'../cuoti_app/static/js/feature-loader.js');
  vm.runInContext(fs.readFileSync(file,'utf8'),ctx);
  const show=route=>{for(const [id,panel] of panels)panel.on=id==='p-'+route;};
  return {features:ctx.Cuoti.features,show};
}
(async()=>{
  for(const outcome of ['resolve','reject']){
    const f=fixture();let finish,firstUnmounts=0,secondUnmounts=0;
    f.features.register('settings',{mount:()=>new Promise((resolve,reject)=>{finish=outcome==='resolve'?resolve:reject;}),refresh(){},unmount(){firstUnmounts++;}});
    f.features.register('exam',{mount(){},refresh(){},unmount(){secondUnmounts++;}});
    f.show('settings');const first=f.features.activate('settings');await tick();
    f.show('exam');assert.equal(await f.features.activate('exam'),true);
    assert.equal(firstUnmounts,1);
    finish(outcome==='reject'?new Error('late failure'):undefined);await first;
    assert.equal(f.features.active(),'exam');assert.equal(secondUnmounts,0);
  }
  {
    const f=fixture();let finish,unmounts=0;
    f.features.register('settings',{mount:()=>new Promise(resolve=>{finish=resolve;}),refresh(){},unmount(){unmounts++;}});
    f.show('settings');const pending=f.features.activate('settings');await tick();
    await f.features.deactivate();finish();await pending;
    assert.equal(f.features.active(),'');assert.equal(unmounts,1);
  }
  {
    const f=fixture();let unmounts=0;
    f.features.register('settings',{mount:async()=>{throw new Error('mount failed');},refresh(){},unmount(){unmounts++;}});
    f.show('settings');assert.equal(await f.features.activate('settings'),false);
    assert.equal(f.features.active(),'');assert.equal(unmounts,1);
  }
  console.log('FEATURE_LOADER_LIFECYCLE_CONTRACT_OK groups=4');
})().catch(error=>{console.error(error);process.exitCode=1;});
