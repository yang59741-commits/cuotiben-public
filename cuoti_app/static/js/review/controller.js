(function initReviewController(){
  'use strict';

  const review=window.Cuoti.review;
  const state=review.state;
  const UNDO_DELAY=4000;
  let queued=null;
  let undoTimer=null;
  let undoToast=null;
  let sessionDone=0;

  function setStatus(message){
    const status=document.getElementById('spaced-review-status');
    if(status)status.textContent=message;
  }

  function setButtonsPending(id,pending){
    const root=document.getElementById('spaced-review-list');
    root?.querySelectorAll(`.js-review[data-id="${safeInteger(id)}"]`).forEach(button=>{
      button.disabled=pending;
      button.setAttribute('aria-disabled',String(pending));
    });
  }

  function appendReviewItems(list,items){
    const tableBody=list.querySelector('.review-table-scroll tbody');
    const cardList=list.querySelector('.review-card-list');
    if(!tableBody||!cardList)return false;
    const rowSource=document.createElement('tbody');
    rowSource.innerHTML=items.map(review.render.row).join('');
    const cardSource=document.createElement('template');
    cardSource.innerHTML=items.map(review.render.card).join('');
    const added=[...rowSource.children,...cardSource.content.children];
    const rows=document.createDocumentFragment();
    while(rowSource.firstChild)rows.appendChild(rowSource.firstChild);
    const cards=document.createDocumentFragment();
    while(cardSource.content.firstChild)cards.appendChild(cardSource.content.firstChild);
    tableBody.appendChild(rows);
    cardList.appendChild(cards);
    void Promise.all(added.map(node=>renderMath(node)));
    return true;
  }

  function mergeUniqueReviewItems(current,incoming){
    const seen=new Set(current.map(item=>item.publicId||`id:${item.id}`));
    const added=[];
    for(const item of incoming){
      const key=item.publicId||`id:${item.id}`;
      if(seen.has(key))continue;
      seen.add(key);
      added.push(item);
    }
    return {items:[...current,...added],added};
  }

  function reconcileReviewItems(list,previousItems){
    const tableBody=list.querySelector('.review-table-scroll tbody');
    const cardList=list.querySelector('.review-card-list');
    if(!tableBody||!cardList)return false;
    const previous=new Map(previousItems.map(item=>[item.id,item]));
    const added=[];
    for(const [parent,render,tag] of [
      [tableBody,review.render.row,'tbody'],[cardList,review.render.card,'div']
    ]){
      const existing=new Map([...parent.children].map(node=>[safeInteger(node.dataset.reviewId),node]));
      let before=parent.firstElementChild;
      for(const item of state.items){
        let node=existing.get(item.id);
        const old=previous.get(item.id);
        // A stable revision retains formula nodes, hint state and keyboard focus.
        if(!node||!old||old.publicId!==item.publicId||old.revision!==item.revision||
          old.subject!==item.subject||old.chapter!==item.chapter||old.dueStatus!==item.dueStatus||old.overdueDays!==item.overdueDays){
          const source=document.createElement(tag);
          source.innerHTML=render(item);
          const replacement=source.firstElementChild;
          if(node){
            window.MathJax?.typesetClear?.([node]);
            if(before===node)before=replacement;
            parent.replaceChild(replacement,node);
          }
          node=replacement;
          added.push(node);
        }
        if(node!==before)parent.insertBefore(node,before);
        before=node.nextElementSibling;
        existing.delete(item.id);
      }
      for(const node of existing.values()){
        window.MathJax?.typesetClear?.([node]);
        node.remove();
      }
    }
    void Promise.all(added.map(node=>renderMath(node)));
    return true;
  }

  function applyView(summary={},options={}){
    const totalCount=state.pager.total;
    const visibleCount=state.items.length;
    const more=document.getElementById('spaced-review-load-more');
    const total=document.getElementById('spaced-review-total');
    const list=document.getElementById('spaced-review-list');
    const exportButton=document.getElementById('spaced-review-export-pdf');
    const stats=document.getElementById('spaced-review-stats');
    if(stats)stats.innerHTML=review.render.stats(totalCount,summary);
    if(more)more.hidden=!state.pager.hasMore;
    if(total)total.textContent=totalCount?`已显示 ${visibleCount} / ${totalCount} 道`:'';
    renderProgress(totalCount);
    if(exportButton)exportButton.disabled=state.exportInFlight||totalCount===0;
    if(!list)return;
    window.Cuoti.reviewEvidence?.bind(list);
    if(!state.items.length){
      window.MathJax?.typesetClear?.([list]);
      list.innerHTML=emptyStateHTML('今天的间隔复习已经完成',{
        icon:'check',description:'当前筛选范围没有到期题目。',
        actionLabel:'查看错题列表',page:'list'
      });
    }else if(!(options.append&&appendReviewItems(list,options.items||[]))&&
      !(options.reconcile&&reconcileReviewItems(list,options.previous||[]))){
      window.MathJax?.typesetClear?.([list]);
      list.innerHTML=review.render.list(state.items);
      void renderMath(list);
    }
    if(!options.reconcile)window.Cuoti.daily.render();
    if(queued)markQueued(queued.id,true);
    setStatus(totalCount?`已加载 ${visibleCount} 道间隔复习题目`:'今天没有到期题目');
  }

  async function loadPage(options={}){
    const append=Boolean(options.append);
    if(append&&(!state.pager.hasMore||!state.pager.nextCursor))return;
    const revision=++state.requestRevision;
    state.requestController?.abort();
    state.requestController=new AbortController();
    if(!append)state.pager.reset();
    const more=document.getElementById('spaced-review-load-more');
    if(!append&&more)more.hidden=true;
    if(more)more.disabled=append;
    try{
      const params=state.pager.params(append);
      if(state.subjectId)params.set('subject_id',state.subjectId);
      if(state.chapterId)params.set('chapter_id',state.chapterId);
      const page=await review.api.fetchReviewPage(
        params,state.requestController.signal
      );
      if(revision!==state.requestRevision)return;
      const incoming=state.pager.apply(page).map(item=>review.render.normalize({
        ...item,pending:state.pendingIds.has(safeInteger(item.id))
      }));
      const merged=append
        ?mergeUniqueReviewItems(state.items,incoming)
        :{items:incoming,added:incoming};
      const previous=state.items;
      state.items=merged.items;
      applyView(page.summary||{},{append,items:merged.added,previous,reconcile:Boolean(options.reconcile)});
      if(!append)void window.Cuoti.reviewBadge?.refresh?.();
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.requestRevision)return;
      showModal('间隔复习读取失败',error.message,'error');
      setStatus('间隔复习题目读取失败');
    }finally{
      if(revision===state.requestRevision&&more)more.disabled=false;
    }
  }

  function renderProgress(remaining){
    const root=document.getElementById('spaced-review-progress');
    if(!root)return;
    const total=sessionDone+Math.max(0,safeInteger(remaining));
    root.hidden=total===0;
    if(!total)return;
    const percent=Math.round(sessionDone/total*100);
    root.innerHTML=`<div class="spaced-review-progress-text"><span>本次已复习 <b>${sessionDone}</b> / ${total} 道</span><span>${percent}%</span></div><div class="spaced-review-progress-track" role="progressbar" aria-label="本次间隔复习进度" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${sessionDone}"><span style="width:${percent}%"></span></div>`;
  }

  function toggleQueuedClass(id,on){
    const root=document.getElementById('spaced-review-list');
    root?.querySelectorAll?.(`[data-review-id="${safeInteger(id)}"]`).forEach(node=>{
      node.classList?.toggle('is-queued',on);
    });
  }

  function markQueued(id,on){
    toggleQueuedClass(id,on);
    setButtonsPending(id,on);
  }

  function flushQueued(){
    const current=queued;
    if(!current)return Promise.resolve();
    queued=null;
    clearTimeout(undoTimer);undoTimer=null;
    if(undoToast?.isConnected)undoToast.querySelector('.toast-close')?.click();
    undoToast=null;
    setButtonsPending(current.id,false);
    // A failed save keeps the card; drop the faded state so it can be retried.
    return submit(current.id,current.result).then(()=>{
      if(queued?.id!==current.id)toggleQueuedClass(current.id,false);
    });
  }

  function undoQueued(){
    const current=queued;
    if(!current)return;
    queued=null;
    clearTimeout(undoTimer);undoTimer=null;undoToast=null;
    markQueued(current.id,false);
    setStatus('已撤销，这道题仍待复习');
  }

  function queueSubmit(id,result){
    const normalizedId=safeInteger(id);
    const item=state.items.find(candidate=>candidate.id===normalizedId);
    if(!item?.publicId||state.pendingIds.has(normalizedId)||queued?.id===normalizedId||!['pass','again'].includes(result))return;
    void flushQueued();
    queued={id:normalizedId,result};
    markQueued(normalizedId,true);
    const label=result==='pass'?'独立答对':'需要再练';
    setStatus(`已选择“${label}”，${UNDO_DELAY/1000} 秒后保存`);
    undoTimer=setTimeout(()=>{void flushQueued()},UNDO_DELAY);
    undoToast=showToast(`${item.qnum} · ${label}，${UNDO_DELAY/1000} 秒后保存。`,'info',{
      title:'复习结果待保存',actionLabel:'撤销',duration:UNDO_DELAY,onAction:undoQueued
    });
  }

  async function submit(id,result){
    const normalizedId=safeInteger(id);
    const item=state.items.find(candidate=>candidate.id===normalizedId);
    if(!item?.publicId||state.pendingIds.has(normalizedId)||!['pass','again'].includes(result))return;
    state.pendingIds.add(normalizedId);
    const mountRevision=state.mountRevision;
    setButtonsPending(normalizedId,true);
    setStatus(result==='pass'?'正在记录本次答对':'正在记录需要再练');
    try{
      await review.api.submitReview(item,result);
      sessionDone+=1;
      if(mountRevision===state.mountRevision)await loadPage({reconcile:true});
    }catch(error){
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT'){
        const showConflict=window.Cuoti.mistakes?.editors?.showRevisionConflict;
        if(showConflict){
          showConflict(error,{onLoadLatest:async()=>{
            await loadPage({reconcile:true});
          },onRetry:async data=>{
            const latestItem={...item,revision:data.currentRevision};
            try{await review.api.submitReview(latestItem,result);if(mountRevision===state.mountRevision)await loadPage({reconcile:true})}
            catch(retryError){showConflict(retryError,{onRetry:()=>submit(id,result)})}
          }});
        }else showModal('复习记录失败',error.message,'error');
      }else showModal('复习记录失败',error.message,'error');
      setStatus('复习结果记录失败');
    }finally{
      state.pendingIds.delete(normalizedId);
      setButtonsPending(normalizedId,false);
    }
  }

  function handleReviewClick(event){
    const button=event.target.closest('.js-review');
    const root=document.getElementById('spaced-review-list');
    if(!button||!root?.contains(button)||button.disabled)return;
    queueSubmit(button.dataset.id,button.dataset.result);
  }

  function triggerDownload(result){
    const href=safeLocalURL(result?.download_url);
    if(!href)throw new Error('下载地址无效');
    const link=document.createElement('a');
    link.href=href;
    link.download=String(result?.download_name||'间隔复习批次.zip');
    link.hidden=true;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function exportReview(layout){
    if(state.exportInFlight||state.pager.total===0)return;
    const button=document.getElementById('spaced-review-export-pdf');
    const status=document.getElementById('spaced-review-export-status');
    state.exportInFlight=true;
    if(button){button.disabled=true;button.innerHTML=icon('loader','icon-spin')+'正在生成...'}
    if(status)status.textContent='正在冻结当前筛选范围的全部到期题目';
    try{
      const filters={review_due:true};
      if(state.subjectId)filters.subject_id=safeInteger(state.subjectId);
      if(state.chapterId)filters.chapter_id=safeInteger(state.chapterId);
      const result=await runJobRequest('mistake-export',{
        export_context:'selected_mistakes',filters,layout,pdf_purpose:document.getElementById('spaced-pdf-purpose')?.value||'self_test'
      });
      triggerDownload(result);
      if(status){
        status.textContent=`已生成 ${safeInteger(result?.item_count)} 道题、${safeInteger(result?.volume_count,1)} 个分册`;
      }
    }catch(error){
      if(status)status.textContent='生成失败';
      showModal('间隔复习导出失败',error.message,'error');
    }finally{
      state.exportInFlight=false;
      if(button){
        button.disabled=state.pager.total===0;
        button.innerHTML=icon('download')+'导出待复习 PDF';
      }
    }
  }

  function chooseExportLayout(){
    if(state.exportInFlight||state.pager.total===0)return;
    showModal(
      '选择间隔复习 PDF 布局',
      '导出当前科目和章节范围内的全部到期题目。',
      'info',
      '<button class="btn btn-out btn-sm" data-action="hide-modal">取消</button>'+
      '<button class="btn btn-out btn-sm" id="review-layout-compact">紧凑</button>'+
      '<button class="btn btn-out btn-sm" id="review-layout-notes">笔记留白</button>'+
      '<button class="btn btn-s btn-sm" id="review-layout-two-column">双栏</button>'
    );
    for(const [id,layout] of [
      ['compact','compact'],['notes','notes'],['two-column','two_column']
    ]){
      document.getElementById('review-layout-'+id)?.addEventListener('click',()=>{
        hideModal();
        void exportReview(layout);
      });
    }
  }

  function optionExists(select,value){
    return [...(select?.options||[])].some(option=>option.value===String(value));
  }

  function resetChapters(){
    const chapter=document.getElementById('spaced-review-chapter');
    if(!chapter)return;
    state.chapterRequestController?.abort();
    state.chapterRequestRevision+=1;
    chapter.replaceChildren(new Option('请先选择科目',''));
    chapter.disabled=true;
    syncCustomSelect(chapter);
  }

  async function loadChapterOptions(subjectId,requestedChapter=''){
    const chapter=document.getElementById('spaced-review-chapter');
    if(!chapter)return;
    if(!subjectId){resetChapters();return}
    const revision=++state.chapterRequestRevision;
    state.chapterRequestController?.abort();
    state.chapterRequestController=new AbortController();
    chapter.replaceChildren(new Option('正在读取章节...',''));
    chapter.disabled=true;
    chapter.setAttribute('aria-busy','true');
    syncCustomSelect(chapter);
    try{
      const chapters=await review.api.fetchChapters(
        subjectId,state.chapterRequestController.signal
      );
      if(revision!==state.chapterRequestRevision)return;
      chapter.replaceChildren(new Option('全部章节',''));
      for(const item of Array.isArray(chapters)?chapters:[]){
        chapter.add(new Option(String(item.name||''),String(safeInteger(item.id,''))));
      }
      chapter.disabled=chapter.options.length===1;
      if(requestedChapter&&optionExists(chapter,requestedChapter)){
        chapter.value=String(requestedChapter);
      }
      state.chapterId=chapter.value;
      syncCustomSelect(chapter);
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.chapterRequestRevision)return;
      chapter.replaceChildren(new Option('章节读取失败',''));
      chapter.disabled=true;
      state.chapterId='';
      syncCustomSelect(chapter);
      const status=document.getElementById('spaced-review-export-status');
      if(status)status.textContent='章节筛选暂不可用，可继续查看该科目全部到期题';
    }finally{
      if(revision===state.chapterRequestRevision)chapter.removeAttribute('aria-busy');
    }
  }

  async function initToolbar(){
    if(state.toolbarPromise)return state.toolbarPromise;
    state.toolbarPromise=(async()=>{
      const subject=document.getElementById('spaced-review-subject');
      const chapter=document.getElementById('spaced-review-chapter');
      try{
        const subjects=await review.api.fetchSubjects();
        for(const item of subjects){
          subject?.add(new Option(String(item.name||''),String(safeInteger(item.id,''))));
        }
        if(subject)syncCustomSelect(subject);
      }catch(error){
        if(subject){subject.disabled=true;syncCustomSelect(subject)}
        const status=document.getElementById('spaced-review-export-status');
        if(status)status.textContent='科目筛选暂不可用，可继续查看全部到期题';
      }
      subject?.addEventListener('change',async()=>{
        state.subjectId=subject.value;
        state.chapterId='';
        await loadChapterOptions(state.subjectId);
        window.Cuoti.reviewPage?.navigate?.('spaced',{replace:true});
      });
      chapter?.addEventListener('change',()=>{
        state.chapterId=chapter.value;
        window.Cuoti.reviewPage?.navigate?.('spaced',{replace:true});
      });
      state.toolbarInitialized=true;
    })();
    return state.toolbarPromise;
  }

  async function applyRouteFilters(params){
    await initToolbar();
    const subject=document.getElementById('spaced-review-subject');
    const requestedSubject=String(params?.get?.('subject_id')||'');
    const requestedChapter=String(params?.get?.('chapter_id')||'');
    state.subjectId=optionExists(subject,requestedSubject)?requestedSubject:'';
    if(subject){subject.value=state.subjectId;syncCustomSelect(subject)}
    state.chapterId='';
    await loadChapterOptions(state.subjectId,requestedChapter);
  }

  function bind(){
    const root=document.getElementById('spaced-review-list');
    if(root&&root.dataset.reviewControllerBound!=='true'){
      root.dataset.reviewControllerBound='true';
      root.addEventListener('click',handleReviewClick);
      window.addEventListener('pagehide',()=>{void flushQueued()});
    }
    const more=document.getElementById('spaced-review-load-more');
    if(more&&more.dataset.reviewControllerBound!=='true'){
      more.dataset.reviewControllerBound='true';
      more.addEventListener('click',()=>void loadPage({append:true}));
    }
    const exportButton=document.getElementById('spaced-review-export-pdf');
    if(exportButton&&exportButton.dataset.reviewControllerBound!=='true'){
      exportButton.dataset.reviewControllerBound='true';
      exportButton.addEventListener('click',chooseExportLayout);
    }
  }

  async function mount(params=new URLSearchParams()){
    const revision=++state.mountRevision;
    sessionDone=0;
    bind();
    await applyRouteFilters(params);
    if(revision!==state.mountRevision)return;
    return loadPage();
  }

  function deactivate(){
    void flushQueued();
    state.mountRevision+=1;
    state.requestRevision+=1;
    state.requestController?.abort();
    state.chapterRequestRevision+=1;
    state.chapterRequestController?.abort();
  }

  review.controller=Object.freeze({
    loadPage,submit,queueSubmit,undoQueued,flushQueued,bind,mount,deactivate,exportReview,chooseExportLayout
  });
})();
