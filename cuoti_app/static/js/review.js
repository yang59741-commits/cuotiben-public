(function initReviewPage(){
  'use strict';

  const spaced=window.Cuoti?.review?.controller;
  const daily=window.Cuoti?.dailyReview?.page;
  if(!spaced||!daily)throw new Error('Review page controllers failed to load');
  let activeMode='';
  let activationRevision=0;
  let bound=false;

  function normalizedMode(params){
    return params?.get?.('mode')==='daily'?'daily':'spaced';
  }

  function filtersFor(mode){
    if(mode==='daily'){
      return {subject_id:window.Cuoti.dailyReview.state.subjectId};
    }
    return {
      subject_id:window.Cuoti.review.state.subjectId,
      chapter_id:window.Cuoti.review.state.chapterId
    };
  }

  function routeParams(mode){
    const params=new URLSearchParams({mode});
    const filters=filtersFor(mode);
    if(filters.subject_id)params.set('subject_id',filters.subject_id);
    if(filters.chapter_id)params.set('chapter_id',filters.chapter_id);
    return params;
  }

  function navigate(mode,{replace=false}={}){
    const targetMode=mode==='daily'?'daily':'spaced';
    const target=routeURL('review',routeParams(targetMode));
    const current=window.location.pathname+window.location.search+window.location.hash;
    if(replace){
      window.history.replaceState({route:'review'},'',target);
      applyHashRoute();
    }else if(target===current){
      applyHashRoute();
    }else{
      window.location.hash=target.slice(target.indexOf('#'));
    }
  }

  function renderMode(mode){
    for(const candidate of ['spaced','daily']){
      const selected=candidate===mode;
      const tab=document.getElementById(`review-tab-${candidate}`);
      const panel=document.getElementById(`review-${candidate}-panel`);
      if(tab){
        tab.classList.toggle('on',selected);
        tab.setAttribute('aria-selected',String(selected));
        tab.tabIndex=selected?0:-1;
      }
      if(panel)panel.hidden=!selected;
    }
  }

  function bind(){
    if(bound)return;
    bound=true;
    const tabs=document.querySelector('.review-mode-tabs');
    tabs?.addEventListener('click',event=>{
      const tab=event.target.closest('[data-review-mode]');
      if(tab)navigate(tab.dataset.reviewMode);
    });
    tabs?.addEventListener('keydown',event=>{
      const items=[...tabs.querySelectorAll('[role="tab"][data-review-mode]')];
      const index=items.indexOf(document.activeElement);
      if(index<0)return;
      let next=index;
      if(event.key==='ArrowRight')next=(index+1)%items.length;
      else if(event.key==='ArrowLeft')next=(index-1+items.length)%items.length;
      else if(event.key==='Home')next=0;
      else if(event.key==='End')next=items.length-1;
      else return;
      event.preventDefault();
      items[next].focus();
      navigate(items[next].dataset.reviewMode);
    });
  }

  async function activate(context={}){
    const revision=++activationRevision;
    const params=context.params instanceof URLSearchParams
      ?context.params:new URLSearchParams();
    const mode=normalizedMode(params);
    if(activeMode&&activeMode!==mode){
      if(activeMode==='daily')daily.unmount();
      else spaced.deactivate();
    }
    activeMode=mode;
    renderMode(mode);
    await window.Cuoti.daily.load();
    if(revision!==activationRevision)return;
    if(mode==='daily')await daily.mount(params);
    else await spaced.mount(params);
  }

  function unmount(){
    activationRevision+=1;
    if(activeMode==='daily')daily.unmount();
    else if(activeMode==='spaced')spaced.deactivate();
    activeMode='';
  }

  bind();
  window.loadReviewPage=(options={})=>activeMode==='daily'
    ?window.Cuoti.dailyReview.controller.load()
    :spaced.loadPage(options);
  window.review=(id,result)=>spaced.submit(id,result);
  window.Cuoti.reviewPage=Object.freeze({navigate,currentMode:()=>activeMode});
  window.Cuoti.features.register('review',Object.freeze({
    mount:activate,
    refresh:activate,
    unmount
  }));
})();
