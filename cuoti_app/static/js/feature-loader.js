(function initFeatureLoader(){
  'use strict';

  window.Cuoti=window.Cuoti||{};

  const FEATURE_TIMEOUT_MS=15000;
  const bundles=Object.freeze({
    add:Object.freeze([
      '/static/js/latex-guide.js',
      '/static/js/mistakes/state.js',
      '/static/js/mistakes/api.js',
      '/static/js/mistakes/render.js',
      '/static/js/mistakes/editors.js',
      '/static/js/mistakes/exports.js',
      '/static/js/mistakes.js'
    ]),
    list:Object.freeze([
      '/static/js/classification.js',
      '/static/js/mistakes/state.js',
      '/static/js/mistakes/api.js',
      '/static/js/mistakes/render.js',
      '/static/js/mistakes/editors.js',
      '/static/js/mistakes/exports.js',
      '/static/js/mistakes.js',
      '/static/js/daily-analysis/state.js',
      '/static/js/daily-analysis/api.js',
      '/static/js/daily-analysis/render.js',
      '/static/js/daily-analysis/controller.js',
      '/static/js/daily-analysis.js'
    ]),
    input:Object.freeze(['/static/js/library.js']),
    review:Object.freeze([
      '/static/js/classification.js',
      '/static/js/review/evidence.js',
      '/static/js/review/state.js',
      '/static/js/review/api.js',
      '/static/js/review/render.js',
      '/static/js/review/controller.js',
      '/static/js/daily-review/state.js',
      '/static/js/daily-review/api.js',
      '/static/js/daily-review/render.js?bundle=practice-feedback-v1',
      '/static/js/daily-review/controller.js?bundle=practice-feedback-v1',
      '/static/js/daily-review.js',
      '/static/js/review.js'
    ]),
    exam:Object.freeze([
      '/static/js/exam/state.js?bundle=practice-feedback-v1',
      '/static/js/exam/api.js?bundle=practice-feedback-v1',
      '/static/js/exam/selection-free.js?bundle=practice-feedback-v1',
      '/static/js/exam/selection-past.js?bundle=practice-feedback-v1',
      '/static/js/exam/selection-mistakes.js?bundle=practice-feedback-v1',
      '/static/js/exam/selection.js?bundle=practice-feedback-v1',
      '/static/js/exam/history.js?bundle=practice-feedback-v1',
      '/static/js/exam/issues.js?bundle=practice-feedback-v1',
      '/static/js/exam/preview.js?bundle=practice-feedback-v1',
      '/static/js/exam/attempts.js?bundle=practice-feedback-v1',
      '/static/js/exam/controller.js?bundle=practice-feedback-v1',
      '/static/js/exam.js?bundle=practice-feedback-v1'
    ]),
    stats:Object.freeze([
      '/static/js/stats/format.js?bundle=stats-redesign-v1',
      '/static/js/stats/charts.js?bundle=stats-redesign-v1',
      '/static/js/stats/render.js?bundle=stats-redesign-v1',
      '/static/js/stats/controller.js?bundle=stats-redesign-v1',
      '/static/js/stats.js?bundle=stats-redesign-v1'
    ]),
    settings:Object.freeze([
      '/static/js/ui/async.js?bundle=settings-redesign-v1',
      '/static/js/settings/state.js?bundle=settings-redesign-v1',
      '/static/js/settings/api.js?bundle=settings-redesign-v1',
      '/static/js/settings/ui.js?bundle=settings-redesign-v1',
      '/static/js/settings/render.js?bundle=settings-redesign-v1',
      '/static/js/settings/connection.js?bundle=settings-redesign-v1',
      '/static/js/settings/routing.js?bundle=settings-redesign-v1',
      '/static/js/settings/memory.js?bundle=settings-redesign-v1',
      '/static/js/settings/diagnostics.js?bundle=settings-redesign-v1',
      '/static/js/settings/maintenance.js?bundle=settings-redesign-v1',
      '/static/js/settings/explainer-scripts.js?bundle=settings-redesign-v1',
      '/static/js/settings/explainer-player.js?bundle=settings-redesign-v1',
      '/static/js/settings/shell.js?bundle=settings-redesign-v1',
      '/static/js/settings/controller.js?bundle=settings-redesign-v1',
      '/static/js/settings.js?bundle=settings-redesign-v1'
    ]),
    data:Object.freeze(['/static/js/data.js'])
  });
  const routeNames=new Set(['add','list','input','review','exam','stats','settings']);
  const assetStates=new Map();
  const bundlePromises=new Map();
  const contracts=new Map();
  let activeRoute='';
  let activationRevision=0;

  function timeout(promise,label){
    let timer=0;
    return Promise.race([
      promise,
      new Promise((resolve,reject)=>{
        timer=window.setTimeout(
          ()=>reject(new Error(`${label} 加载超时，请检查网络后重试。`)),
          FEATURE_TIMEOUT_MS
        );
      })
    ]).finally(()=>window.clearTimeout(timer));
  }

  function startAsset(url){
    const existing=assetStates.get(url);
    if(existing?.status==='loaded'||existing?.status==='pending')return existing.promise;
    existing?.element?.remove();
    const script=document.createElement('script');
    script.src=url;
    script.async=false;
    script.dataset.cuotiFeatureAsset=url;
    const state={status:'pending',element:script,promise:null};
    state.promise=new Promise((resolve,reject)=>{
      script.addEventListener('load',()=>{
        state.status='loaded';
        resolve(url);
      },{once:true});
      script.addEventListener('error',()=>{
        state.status='failed';
        reject(new Error(`无法加载 ${url.split('/').at(-1)}`));
      },{once:true});
    });
    assetStates.set(url,state);
    document.head.appendChild(script);
    return state.promise;
  }

  async function loadAssets(urls){
    for(const url of urls)await timeout(startAsset(url),url);
  }

  function clearIncompleteAssets(urls){
    for(const url of urls){
      const state=assetStates.get(url);
      if(!state||state.status==='loaded')continue;
      state.element?.remove();
      assetStates.delete(url);
    }
  }

  function register(name,contract){
    if(!bundles[name])throw new Error(`未知功能：${name}`);
    if(!contract||typeof contract.mount!=='function'||
       typeof contract.unmount!=='function'||typeof contract.refresh!=='function'){
      throw new TypeError(`${name} 必须实现 mount/unmount/refresh`);
    }
    if(contracts.has(name))return contracts.get(name);
    const frozen=Object.freeze({...contract});
    contracts.set(name,frozen);
    return frozen;
  }

  async function load(name,{retry=false}={}){
    if(!bundles[name])throw new Error(`未知功能：${name}`);
    if(retry){
      bundlePromises.delete(name);
      clearIncompleteAssets(bundles[name]);
    }
    let promise=bundlePromises.get(name);
    if(!promise){
      const started=performance.now();
      const alreadyLoaded=bundles[name].every(url=>assetStates.get(url)?.status==='loaded');
      promise=loadAssets(bundles[name]).then(()=>{
        const contract=contracts.get(name);
        if(!contract)throw new Error(`${name} 未注册页面生命周期`);
        window.dispatchEvent(new CustomEvent('cuoti:feature-loaded',{detail:Object.freeze({
          name,
          cached:alreadyLoaded,
          assets:bundles[name].length,
          duration_ms:Math.round((performance.now()-started)*1000)/1000
        })}));
        return contract;
      }).catch(error=>{
        bundlePromises.delete(name);
        clearIncompleteAssets(bundles[name]);
        throw error;
      });
      bundlePromises.set(name,promise);
    }
    return timeout(promise,name);
  }

  function panelState(route){
    const panel=document.getElementById(`p-${route}`);
    if(!panel)return null;
    let status=panel.querySelector(':scope > .feature-load-state');
    if(!status){
      status=document.createElement('div');
      status.className='feature-load-state';
      status.hidden=true;
      status.setAttribute('role','status');
      status.setAttribute('aria-live','polite');
      panel.prepend(status);
    }
    return {panel,status};
  }

  function setLoading(route,loading){
    const state=panelState(route);
    if(!state)return;
    state.panel.toggleAttribute('aria-busy',loading);
    state.status.hidden=!loading;
    if(loading){
      state.status.className='feature-load-state';
      state.status.replaceChildren(document.createTextNode('正在加载本页功能…'));
    }
  }

  function showFailure(route,error){
    const state=panelState(route);
    if(!state)return;
    state.panel.removeAttribute('aria-busy');
    state.status.hidden=false;
    state.status.className='feature-load-state error';
    const message=document.createElement('span');
    message.textContent=`本页功能加载失败：${error.message}`;
    const retry=document.createElement('button');
    retry.type='button';
    retry.className='btn btn-out btn-sm';
    retry.textContent='重试';
    retry.addEventListener('click',()=>void activate(route,{retry:true}));
    state.status.replaceChildren(message,retry);
  }

  async function unmountActive(){
    if(!activeRoute)return;
    const previous=contracts.get(activeRoute);
    activeRoute='';
    try{await previous?.unmount?.()}catch(error){console.warn('Feature unmount failed:',error.message)}
  }

  async function deactivate(){
    activationRevision+=1;
    await unmountActive();
  }

  async function activate(route,context={}){
    if(!routeNames.has(route))return false;
    const revision=++activationRevision;
    const sameRoute=activeRoute===route;
    if(!sameRoute)await unmountActive();
    setLoading(route,true);
    try{
      const contract=await load(route,{retry:Boolean(context.retry)});
      if(revision!==activationRevision||!document.getElementById(`p-${route}`)?.classList.contains('on'))return false;
      // Ownership starts before mount awaits data, so navigation can unmount it.
      activeRoute=route;
      await contract[sameRoute?'refresh':'mount']({...context,route});
      if(revision!==activationRevision)return false;
      activeRoute=route;
      const state=panelState(route);
      if(state){state.panel.removeAttribute('aria-busy');state.status.hidden=true}
      window.dispatchEvent(new CustomEvent('cuoti:route-active',{detail:Object.freeze({route,revision})}));
      return true;
    }catch(error){
      if(revision===activationRevision){await unmountActive();if(revision===activationRevision)showFailure(route,error)}
      console.error('Feature activation failed:',error);
      return false;
    }
  }

  window.Cuoti.features=Object.freeze({
    activate,
    deactivate,
    load,
    register,
    active:()=>activeRoute,
    snapshot:()=>Object.freeze({
      active:activeRoute,
      loaded:Object.freeze([...assetStates].filter(([,state])=>state.status==='loaded').map(([url])=>url)),
      registered:Object.freeze([...contracts.keys()])
    })
  });
})();
