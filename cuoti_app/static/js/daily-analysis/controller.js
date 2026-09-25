(function initDailyAnalysisController(){
  'use strict';

  const feature=window.Cuoti.dailyAnalysis;
  const state=feature.state;

  function setTrigger(trigger,pending,label='分析当日'){
    if(!trigger?.isConnected)return;
    trigger.disabled=pending;
    trigger.innerHTML=pending?icon('loader','icon-spin')+'分析中...':icon('sparkles')+label;
  }

  async function waitForExternalGeneration(date,mistakePublicId,revision){
    for(let attempt=0;attempt<30;attempt+=1){
      await new Promise(resolve=>{window.setTimeout(resolve,1000)});
      if(revision!==state.requestRevision)return null;
      const payload=await feature.api.fetchCache(date,mistakePublicId);
      if(!payload.in_progress)return payload;
    }
    throw new Error('分析仍在其他窗口生成，请稍后重新打开');
  }

  async function ensureCache(date,mistakePublicId,scope,revision){
    let payload=await feature.api.fetchCache(date,mistakePublicId);
    if(revision!==state.requestRevision)return null;
    if(payload.in_progress){
      payload=await waitForExternalGeneration(date,mistakePublicId,revision);
    }else if(payload.missing_count>0){
      await feature.api.generate(date,scope,mistakePublicId);
      if(revision!==state.requestRevision)return null;
      payload=await feature.api.fetchCache(date,mistakePublicId);
      if(payload.in_progress)payload=await waitForExternalGeneration(date,mistakePublicId,revision);
    }
    return payload;
  }

  function present(payload,date,mistakePublicId=''){
    const single=Boolean(mistakePublicId);
    const title=single?'单题每日分析':`${date} 当日分析`;
    const actionLabel=single?'重新生成本题':'重新生成全部';
    showModal(title,'','info',
      `<button type="button" class="btn btn-out btn-sm" id="daily-analysis-regenerate">${icon('refresh')}${actionLabel}</button>`+
      '<button type="button" class="btn btn-p btn-sm" onclick="hideModal()">关闭</button>');
    document.querySelector('#modal .modal-box')?.classList.add('modal-box-wide');
    const body=document.getElementById('modal-body');
    body.className='modal-body daily-analysis-modal';
    body.innerHTML=feature.render.body(payload);
    body.querySelectorAll('.js-analysis-practice-type').forEach(link=>link.addEventListener('click',event=>{
      event.preventDefault();
      const target=link.getAttribute('href');
      hideModal(()=>{window.location.hash=target});
    }));
    body.querySelectorAll('.js-analysis-self-check').forEach(button=>button.addEventListener('click',async()=>{
      const card=button.closest('[data-mistake-public-id]');
      const item=payload.items.find(row=>row.mistake_public_id===card.dataset.mistakePublicId);
      const text=card.querySelector('.analysis-self-check').value.trim();
      if(!text)return;
      const line='下次自查：'+text;
      if(String(item.note||'').split('\n').includes(line)){button.textContent='已记录';return}
      button.disabled=true;
      try{
        const note=[item.note,line].filter(Boolean).join('\n');
        const saved=await api('/api/v2/mistakes/'+encodeURIComponent(item.mistake_public_id),{method:'PATCH',body:JSON.stringify({revision:item.revision,note})});
        item.note=saved.note;item.revision=saved.revision;button.textContent='已记录';
      }catch(error){showModal('记录未保存',error.message,'error')}finally{button.disabled=false}
    }));
    if(payload.classification_missing_count){
      body.insertAdjacentHTML('afterbegin',`<div class="classification-notice">${safeInteger(payload.classification_missing_count)} 道题尚无有效分类 <button type="button" class="btn btn-out btn-sm" id="daily-analysis-classify">补充分类</button></div>`);
      document.getElementById('daily-analysis-classify')?.addEventListener('click',async()=>{
        try{await feature.api.generate(date,single?'single':'missing',mistakePublicId);await open(date,mistakePublicId)}catch(error){showModal('分类失败',error.message,'error')}
      });
    }
    void renderMath(body);
    document.getElementById('daily-analysis-regenerate')?.addEventListener('click',()=>{
      void regenerate(date,mistakePublicId);
    });
  }

  async function open(date,mistakePublicId='',trigger=null){
    if(state.pending)return;
    const revision=++state.requestRevision;
    state.pending=true;
    setTrigger(trigger,true,mistakePublicId?'单题分析':'分析当日');
    try{
      const payload=await ensureCache(
        date,mistakePublicId,mistakePublicId?'single':'missing',revision
      );
      if(payload&&revision===state.requestRevision)present(payload,date,mistakePublicId);
    }catch(error){
      if(revision===state.requestRevision)showModal('分析失败',error.message,'error');
    }finally{
      if(revision===state.requestRevision)state.pending=false;
      setTrigger(trigger,false,mistakePublicId?'单题分析':'分析当日');
    }
  }

  async function regenerate(date,mistakePublicId=''){
    if(state.pending)return;
    const revision=++state.requestRevision;
    const button=document.getElementById('daily-analysis-regenerate');
    state.pending=true;
    if(button){button.disabled=true;button.innerHTML=icon('loader','icon-spin')+'重新生成中...'}
    try{
      await feature.api.generate(date,mistakePublicId?'single':'all',mistakePublicId);
      if(revision!==state.requestRevision)return;
      let payload=await feature.api.fetchCache(date,mistakePublicId);
      if(payload.in_progress){
        payload=await waitForExternalGeneration(date,mistakePublicId,revision);
      }
      present(payload,date,mistakePublicId);
    }catch(error){
      if(revision===state.requestRevision)showModal('重新生成失败',error.message,'error');
    }finally{
      if(revision===state.requestRevision)state.pending=false;
    }
  }

  feature.controller=Object.freeze({open,regenerate,present,ensureCache});
})();
