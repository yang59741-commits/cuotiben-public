(function initMistakeController(){
  'use strict';

  const state=window.Cuoti.mistakes.state;
  const apiAdapter=window.Cuoti.mistakes.api;
  const renderer=window.Cuoti.mistakes.render;
  const editors=window.Cuoti.mistakes.editors;
  const exportsController=window.Cuoti.mistakes.exports;
  const LIST_PREFERENCES_KEY='cuoti.list-preferences.v1';

  function readListPreferences(){
    try{
      const value=JSON.parse(localStorage.getItem(LIST_PREFERENCES_KEY)||'null');
      return value?.schema===1&&value.filters&&typeof value.filters==='object'
        ?value.filters:{};
    }catch(error){return {}}
  }

  function getListRouteParams(){
    if(!state.filtersInitialized)return new URLSearchParams(readListPreferences());
    const params=new URLSearchParams();
    if(/^\d+$/.test(state.subjectFilter))params.set('subject',state.subjectFilter);
    const chapter=String(document.getElementById('f-chapter')?.value||'');
    const status=String(document.getElementById('f-status')?.value||'');
    const search=String(document.getElementById('f-search')?.value||'').trim();
    if(/^\d+$/.test(chapter))params.set('chapter',chapter);
    if(status&&status!=='全部')params.set('status',status);
    if(search)params.set('search',search.slice(0,200));
    return params;
  }

  function syncListNavigation(){
    state.filtersInitialized=true;
    const params=getListRouteParams();
    try{
      localStorage.setItem(LIST_PREFERENCES_KEY,JSON.stringify({
        schema:1,filters:Object.fromEntries(params),updated_at:new Date().toISOString()
      }));
    }catch(error){}
    if(window.location.hash.startsWith('#/list')){
      const url=new URL(window.location.href);
      const query=params.toString();
      url.hash='#/list'+(query?'?'+query:'');
      history.replaceState(history.state,'',url.pathname+url.search+url.hash);
    }
  }

  async function applyListRoute(routeParams){
    const supplied=[...routeParams.keys()].length
      ?Object.fromEntries(routeParams):readListPreferences();
    state.filtersInitialized=true;
    state.subjectFilter=/^\d+$/.test(String(supplied.subject||''))
      ?String(supplied.subject):'';
    const status=document.getElementById('f-status');
    const search=document.getElementById('f-search');
    const requestedStatus=String(supplied.status||'');
    if(status){
      status.value=[...status.options].some(option=>option.value===requestedStatus)
        ?requestedStatus:'全部';
      syncCustomSelect(status);
    }
    if(search)search.value=String(supplied.search||'').slice(0,200);
    const chapter=document.getElementById('f-chapter');
    if(state.subjectFilter){
      await loadChapters('f',state.subjectFilter,{load:false});
      const requested=String(supplied.chapter||'');
      if(chapter&&[...chapter.options].some(option=>option.value===requested)){
        chapter.value=requested;
        syncCustomSelect(chapter);
      }
    }else if(chapter){
      resetChapterSelect('f');
    }
    syncListNavigation();
    await loadMistakes();
  }

  function bindLengthCounter(inputId,countId,maximum){
    const input=document.getElementById(inputId);
    const count=document.getElementById(countId);
    if(!input||!count)return;
    const update=()=>{
      const length=Array.from(input.value).length;
      count.textContent=`${length} / ${maximum}`;
      count.classList.toggle('near-limit',length>=Math.floor(maximum*.9));
    };
    input.addEventListener('input',update);update();
  }

  function refreshMistakeEntryCounters(){
    ['s-problem','s-note'].forEach(id=>
      document.getElementById(id)?.dispatchEvent(new Event('input'))
    );
  }

  function hasMultipleNumberedProblems(value){
    const text=String(value??'').replace(/\r\n?/g,'\n');
    const starts=[...text.matchAll(
      /^[ \t]*(?:[（(]?\d{1,2}[）).、．]|(?:题目|问题)\s*\d+|第\s*\d+\s*题|%\s*\d+(?:[.、)]|\s+)|\\text\s*\{\s*\d{1,2}[.、．)]|\\subsection\b)/gm
    )];
    const commonStem=starts.length>0&&text.slice(0,starts[0].index).trim();
    const subpartNumbers=starts.map(match=>{
      const marker=match[0].match(/^[ \t]*[（(](\d{1,2})[）)]/);
      return marker?Number(marker[1]):null;
    });
    const parenthesizedSequence=subpartNumbers.length>1&&
      subpartNumbers.every((number,index)=>number===index+1);
    if(commonStem&&parenthesizedSequence)return false;
    return starts.length>1;
  }

  async function add(){
    const sid=document.getElementById('s-subject').value;
    const cid=document.getElementById('s-chapter').value;
    const qnum=document.getElementById('s-qnum').value.trim();
    const content=document.getElementById('s-problem').value.trim();
    if(!content){showModal('提示','请粘贴题目内容','info');return}
    const shouldBatch=!sid||hasMultipleNumberedProblems(content);
    if(shouldBatch){
      const button=document.querySelector('#p-add .btn-p');
      button.disabled=true;
      button.innerHTML=icon('loader','icon-spin')+
        (sid?'正在拆分并保存...':'AI 正在拆题与归类...');
      const reasons=selectedReasons.join(',');
      const note=document.getElementById('s-note').value.trim();
      const date=document.getElementById('s-date').value;
      const controller=new AbortController();
      const timeoutId=setTimeout(()=>controller.abort(),28000);
      try{
        const payload={
          content,reasons,note,date_added:date,
          subject_id:sid?+sid:null,chapter_id:cid?+cid:null
        };
        const result=await runJobRequest('batch-mistakes',payload);
        if(result.error){
          showModal('分类失败',result.error+'。原题目已保留，请重试。','error');
          return;
        }
        showToast('AI 已拆分并按章节保存 '+result.count+' 道错题','success');
        document.getElementById('s-problem').value='';
        refreshMistakeEntryCounters();
        window.clearRemoteDraftPanel?.('p-add');
        selectedReasons=[];
        document.querySelectorAll('.tag.on').forEach(tag=>{
          tag.classList.remove('on');tag.setAttribute('aria-pressed','false');
        });
      }catch(error){
        const message=error.name==='AbortError'
          ?'分析超过 28 秒，已停止等待。原题目已保留，可直接重试。'
          :'保存失败: '+error.message+'。原题目已保留，请重试。';
        showModal('分类失败',message,'error');
      }finally{
        clearTimeout(timeoutId);button.disabled=false;
        button.innerHTML=icon('check')+'标记错题';
      }
      return;
    }
    try{
      const result=await apiAdapter.create({
        subject_id:+sid,chapter_id:cid?+cid:null,qnum,content,
        reasons:selectedReasons,note:document.getElementById('s-note').value.trim(),
        date_added:document.getElementById('s-date').value
      });
      if(result?.error)throw new Error(result.error);
      document.getElementById('s-qnum').value='';
      document.getElementById('s-note').value='';
      document.getElementById('s-problem').value='';
      refreshMistakeEntryCounters();window.clearRemoteDraftPanel?.('p-add');
      selectedReasons=[];
      document.querySelectorAll('.tag.on').forEach(tag=>{
        tag.classList.remove('on');tag.setAttribute('aria-pressed','false');
      });
      showToast('错题已记录，将在 3 天后开始复习。','success');
    }catch(error){showModal('标记失败',error.message,'error')}
  }

  function setMistakeLoading(loading,append=false){
    const list=document.getElementById('mistake-list');
    const spinner=document.getElementById('search-spinner');
    const more=document.getElementById('mistake-load-more');
    if(list)list.setAttribute('aria-busy',String(loading));
    if(spinner)spinner.hidden=!loading;
    if(more)more.disabled=loading&&append;
  }

  function scheduleMistakeSearch(){
    syncListNavigation();window.clearTimeout(state.searchTimer);
    state.searchTimer=window.setTimeout(()=>loadMistakes(),300);
  }

  function mergeUniqueMistakes(current,incoming,{append=false}={}){
    const merged=append?[...current]:[];
    const seenPublicIds=new Set(
      merged.map(item=>String(item?.public_id||'')).filter(Boolean)
    );
    const seenFallbackIds=new Set(
      merged.filter(item=>!item?.public_id).map(item=>safeInteger(item?.id)).filter(Boolean)
    );
    const added=[];
    for(const item of incoming){
      const publicId=String(item?.public_id||'');
      const fallbackId=safeInteger(item?.id);
      if(publicId?seenPublicIds.has(publicId):(fallbackId&&seenFallbackIds.has(fallbackId)))continue;
      if(publicId)seenPublicIds.add(publicId);
      else if(fallbackId)seenFallbackIds.add(fallbackId);
      merged.push(item);added.push(item);
    }
    return {items:merged,added};
  }

  async function loadMistakes(options={}){
    if(state.view==='trash')return loadTrash();
    const append=Boolean(options.append);
    if(append&&(!state.pager.hasMore||!state.pager.nextCursor))return;
    const revision=++state.requestRevision;
    if(state.requestController)state.requestController.abort();
    state.requestController=new AbortController();
    if(!append)state.pager.reset();
    const status=document.getElementById('mistake-list-status');
    setMistakeLoading(true,append);
    if(status)status.textContent=append?'正在加载更多错题...':'正在读取错题...';
    try{
      const params=state.pager.params(append);
      if(state.subjectFilter)params.set('subject_id',state.subjectFilter);
      const chapter=document.getElementById('f-chapter').value;
      const filterStatus=document.getElementById('f-status').value;
      const search=document.getElementById('f-search').value.trim();
      if(chapter)params.set('chapter_id',chapter);
      if(filterStatus!=='全部')params.set('status',filterStatus);
      if(search)params.set('search',search);
      const payload=await apiAdapter.list(params,state.requestController.signal);
      if(revision!==state.requestRevision)return;
      const paged=payload&&!Array.isArray(payload)&&Array.isArray(payload.items);
      const data=paged?payload.items:(Array.isArray(payload)?payload:[]);
      const merged=mergeUniqueMistakes(state.items,data,{append});
      state.items=merged.items;
      state.pager.nextCursor=paged&&payload.next_cursor!=null?String(payload.next_cursor):null;
      state.pager.hasMore=Boolean(paged&&payload.has_more&&state.pager.nextCursor);
      state.pager.total=Number.isFinite(Number(payload?.total))
        ?Math.max(0,Number(payload.total)):state.items.length;
      if(!append)void refreshSubjectChips();
      await renderMistakes(append?merged.added:state.items,{append});
      const more=document.getElementById('mistake-load-more');
      const total=document.getElementById('mistake-total');
      if(more)more.hidden=!state.pager.hasMore;
      if(total)total.textContent=state.pager.total
        ?`已显示 ${state.items.length} / ${state.pager.total} 道`:'';
      if(status){
        const backend=String(payload?.search_backend||'none');
        const searchNote=backend==='like_explicit_score'?'（LIKE 降级搜索）':
          (backend.startsWith('fts5_')?'（FTS5 相关度搜索）':'');
        status.textContent=state.items.length
          ?`已加载 ${state.items.length} 道错题${searchNote}`
          :`没有符合条件的错题${searchNote}`;
      }
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.requestRevision)return;
      if(append&&error?.data?.code==='INVALID_CURSOR'){
        state.pager.reset();return loadMistakes({append:false});
      }
      console.error('loadMistakes error:',error);
      if(status){
        status.innerHTML=`读取失败：${escapeHTML(error.message)} <button type="button" class="inline-retry" id="mistake-retry">重试</button>`;
      }
      document.getElementById('mistake-retry')?.addEventListener('click',
        ()=>loadMistakes({append}));
      if(!append&&!state.items.length){
        document.getElementById('mistake-list').innerHTML=emptyStateHTML(
          '错题读取失败',{icon:'circleHelp',description:'请使用上方重试按钮重新读取。'}
        );
      }
    }finally{
      if(revision===state.requestRevision)setMistakeLoading(false,append);
    }
  }

  function updateTrashSummary(){
    const loaded=state.items.length,total=state.trashPager.total;
    const status=document.getElementById('mistake-list-status');
    const more=document.getElementById('mistake-load-more');
    const totalNode=document.getElementById('mistake-total');
    if(status)status.textContent=total?`回收站：已加载 ${loaded} / ${total} 道错题`:'回收站为空';
    if(more)more.hidden=!state.trashPager.hasMore;
    if(totalNode)totalNode.textContent=total?`已显示 ${loaded} / ${total} 道`:'';
  }

  async function renderTrashItems(items=state.items,{append=false}={}){
    const root=document.getElementById('mistake-list');bindMistakeListEvents(root);
    if(!append){root.innerHTML=renderer.trashList(items);await renderMath(root);return}
    const template=document.createElement('template');
    template.innerHTML=renderer.trashList(items);
    const incoming=template.content.querySelector('.trash-list');
    const target=root.querySelector(':scope > .trash-list');
    if(!incoming||!target){root.innerHTML=renderer.trashList(state.items);await renderMath(root);return}
    const fragment=document.createDocumentFragment();
    const added=[...incoming.children];
    added.forEach(node=>fragment.appendChild(node));
    target.appendChild(fragment);
    await Promise.all(added.map(node=>renderMath(node)));
  }

  async function loadTrash(options={}){
    const append=Boolean(options.append);
    const revision=++state.requestRevision;
    if(state.requestController)state.requestController.abort();
    state.requestController=new AbortController();
    const status=document.getElementById('mistake-list-status');
    if(!append)state.trashPager.reset();
    setMistakeLoading(true,append);
    if(status)status.textContent=append?'正在加载更多回收站错题...':'正在读取回收站...';
    try{
      const params=state.trashPager.params(append);
      const payload=await apiAdapter.trash(params,state.requestController.signal);
      if(revision!==state.requestRevision)return;
      const hasItems=payload&&!Array.isArray(payload)&&Array.isArray(payload.items);
      const paged=hasItems&&(
        Object.hasOwn(payload,'next_cursor')||Object.hasOwn(payload,'has_more')||
        Object.hasOwn(payload,'total')
      );
      const data=paged?state.trashPager.apply(payload):
        (Array.isArray(payload)?payload:(hasItems?payload.items:[]));
      if(!paged){state.trashPager.reset();state.trashPager.total=data.length}
      state.items=append?[...state.items,...data]:data;
      await renderTrashItems(append?data:state.items,{append});updateTrashSummary();
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.requestRevision)return;
      if(append&&error?.data?.code==='INVALID_CURSOR'){
        state.trashPager.reset();return loadTrash({append:false});
      }
      if(status){
        status.innerHTML=`回收站读取失败：${escapeHTML(error.message)} <button type="button" class="inline-retry" id="mistake-retry">重试</button>`;
      }
      document.getElementById('mistake-retry')?.addEventListener('click',
        ()=>loadTrash({append}));
    }finally{
      if(revision===state.requestRevision)setMistakeLoading(false,append);
    }
  }

  async function discardTrashItem(id){
    if(state.view!=='trash')return;
    state.requestRevision+=1;
    if(state.requestController)state.requestController.abort();
    state.requestController=null;
    setMistakeLoading(false,false);
    const previousLength=state.items.length;
    state.items=state.items.filter(item=>safeInteger(item.id)!==safeInteger(id));
    if(state.items.length===previousLength)return;
    state.trashPager.total=Math.max(0,state.trashPager.total-1);
    if(state.items.length>=state.trashPager.total){
      state.trashPager.hasMore=false;state.trashPager.nextCursor=null;
    }
    await renderTrashItems();
    if(state.view==='trash')updateTrashSummary();
  }

  async function setListView(view){
    state.view=view==='trash'?'trash':'current';
    for(const [name,id] of [['current','mistake-view-current'],['trash','mistake-view-trash']]){
      const button=document.getElementById(id),active=name===state.view;
      button?.classList.toggle('on',active);button?.setAttribute('aria-selected',String(active));
    }
    for(const id of ['subject-chips','f-chapter','f-status','f-search']){
      const node=document.getElementById(id);if(node)node.hidden=state.view==='trash';
    }
    const exportBar=document.getElementById('mistake-export-bar');
    if(exportBar)exportBar.hidden=state.view==='trash';
    state.selectedPublicIds.clear();exportsController.updateBar();
    if(state.view==='trash'){
      state.trashPager.reset();await loadTrash();
    }else{
      state.pager.reset();await loadMistakes();
    }
  }

  function bindMistakeListEvents(root){
    if(!root||root.dataset.mistakeControllerBound)return;
    root.dataset.mistakeControllerBound='1';
    root.addEventListener('click',event=>{
      const control=event.target.closest('button');
      if(!control||!root.contains(control)||control.disabled)return;
      const id=safeInteger(control.dataset.id);
      if(control.matches('.js-analyze-day'))void window.Cuoti.dailyAnalysis.controller.open(control.dataset.date,'',control);
      else if(control.matches('.js-open-daily-analysis'))void window.Cuoti.dailyAnalysis.controller.open(control.dataset.date,control.dataset.publicId,control);
      else if(control.matches('.js-chapter-toggle'))void editors.toggleChapter(id,control);
      else if(control.matches('.js-chapter-save'))void editors.saveChapter(id);
      else if(control.matches('.js-chapter-cancel'))editors.cancelChapter(id);
      else if(control.matches('.js-reason-toggle'))editors.toggleReason(id,control);
      else if(control.matches('.js-reason-choice'))void editors.updateReason(id,control);
      else if(control.matches('.js-note-toggle'))editors.toggleNote(id,control);
      else if(control.matches('.js-note-save'))void editors.saveNote(id);
      else if(control.matches('.js-note-cancel'))editors.cancelNote(id);
      else if(control.matches('.js-note-load-latest'))editors.resolveNoteConflict(id,true);
      else if(control.matches('.js-note-keep-draft'))editors.resolveNoteConflict(id,false);
      else if(control.matches('.js-toggle-mistake'))void editors.toggleStatus(id);
      else if(control.matches('.js-delete-mistake'))void editors.remove(id);
      else if(control.matches('.js-attachments-toggle'))void editors.toggleAttachments(id,control);
      else if(control.matches('.js-attachment-pick'))root.querySelector(`.js-attachment-input[data-id="${id}"]`)?.click();
      else if(control.matches('.js-restore-mistake'))void editors.restore(id);
      else if(control.matches('.js-purge-mistake'))void editors.purge(id);
    });
    root.addEventListener('change',event=>{
      const attachment=event.target.closest('.js-attachment-input');
      if(attachment&&root.contains(attachment)){
        void editors.uploadAttachment(safeInteger(attachment.dataset.id),attachment);return;
      }
      const input=event.target.closest('.js-select-mistake');
      if(input&&root.contains(input)){
        exportsController.setSelected(safeInteger(input.dataset.id),input.checked,input);
      }
    });
    root.addEventListener('input',event=>{
      const input=event.target.closest('.mc-note-editor textarea');
      if(!input||!root.contains(input))return;
      const count=input.closest('.mc-note-editor')?.querySelector('.mc-note-count span');
      if(count)count.textContent=String(input.value.length);
    });
  }

  function refreshMistakeGroupCounts(root){
    root.querySelectorAll('.subject-group').forEach(group=>{
      const count=group.querySelectorAll(':scope > .mistake-card').length;
      const label=group.querySelector(':scope > .subject-header .subject-count');
      if(label)label.textContent=`${count}题`;
    });
    root.querySelectorAll('.date-group').forEach(group=>{
      const cards=[...group.querySelectorAll('.mistake-card')];
      const label=group.querySelector(':scope > .date-header .date-stat');
      if(label)label.textContent=`${cards.length}题 · ${cards.filter(card=>!card.classList.contains('done')).length}未掌握`;
    });
  }

  async function appendMistakes(root,data){
    const template=document.createElement('template');
    template.innerHTML=renderer.list(data,{
      selectedPublicIds:state.selectedPublicIds,reasonOptions:state.reasonOptions
    });
    const added=[];
    const incomingDates=[...template.content.children]
      .filter(node=>node.classList.contains('date-group'));
    for(const incomingDate of incomingDates){
      const existingDate=[...root.querySelectorAll(':scope > .date-group')]
        .find(group=>group.dataset.date===incomingDate.dataset.date);
      if(!existingDate){added.push(incomingDate);root.appendChild(incomingDate);continue}
      for(const incomingSubject of [...incomingDate.querySelectorAll(':scope > .subject-group')]){
        const existingSubject=[...existingDate.querySelectorAll(':scope > .subject-group')]
          .find(group=>group.dataset.subject===incomingSubject.dataset.subject);
        if(!existingSubject){added.push(incomingSubject);existingDate.appendChild(incomingSubject);continue}
        const fragment=document.createDocumentFragment();
        for(const card of [...incomingSubject.querySelectorAll(':scope > .mistake-card')]){
          added.push(card);fragment.appendChild(card);
        }
        existingSubject.appendChild(fragment);
      }
    }
    refreshMistakeGroupCounts(root);
    root.querySelectorAll('.mc-note-editor textarea').forEach(input=>{
      if(added.some(node=>node===input||node.contains(input)))input.dispatchEvent(new Event('input',{bubbles:true}));
    });
    exportsController.updateBar();
    await Promise.all(added.map(node=>renderMath(node)));
  }

  async function renderMistakes(data,{append=false}={}){
    const root=document.getElementById('mistake-list');
    bindMistakeListEvents(root);
    if(append&&data.length&&root.querySelector('.mistake-card')){
      await appendMistakes(root,data);
      return;
    }
    root.innerHTML=renderer.list(data,{
      selectedPublicIds:state.selectedPublicIds,reasonOptions:state.reasonOptions
    });
    root.querySelectorAll('.mc-note-editor textarea').forEach(input=>
      input.dispatchEvent(new Event('input',{bubbles:true}))
    );
    exportsController.updateBar();await renderMath(root);
  }

  async function loadMistakeCountSummary(){
    const status=document.getElementById('f-status')?.value||'';
    try{
      const summary=await apiAdapter.counts(status);
      if(summary&&Array.isArray(summary.subjects))return summary;
    }catch(error){
      console.warn('Mistake counts could not be loaded:',error?.message||error);
      return {total:0,subjects:[]};
    }
  }

  async function refreshSubjectChips(){
    renderSubjectChips(await loadMistakeCountSummary());
  }

  function renderSubjectChips(summary){
    const root=document.getElementById('subject-chips');if(!root)return;
    const subjects=Array.isArray(summary?.subjects)?summary.subjects:[];
    const total=Number(summary?.total)||0;
    let html='<button type="button" class="subject-chip js-subject-chip'+
      (state.subjectFilter===''?' on':'')+'" data-subject="" aria-pressed="'+
      (state.subjectFilter===''?'true':'false')+'">全部<span class="sc-count">('+total+')</span></button>';
    subjects.forEach(subject=>{
      const count=Number(subject.count)||0;
      const subjectId=safeInteger(subject.id);
      const active=state.subjectFilter===String(subjectId);
      html+='<button type="button" class="subject-chip js-subject-chip'+
        (active?' on':'')+'" data-subject="'+subjectId+'" aria-pressed="'+active+'">'+
        escapeHTML(subject.name)+'<span class="sc-count">('+count+')</span></button>';
    });
    root.innerHTML=html;
    root.querySelectorAll('.js-subject-chip').forEach(chip=>{
      chip.addEventListener('click',()=>filterSubject(chip.dataset.subject));
    });
  }

  async function filterSubject(subjectId){
    state.subjectFilter=subjectId;
    if(subjectId){
      await loadChapters('f',subjectId,{load:false});
      document.getElementById('f-chapter').value='';
      syncCustomSelect(document.getElementById('f-chapter'));
    }else{
      resetChapterSelect('f');
    }
    syncListNavigation();void loadMistakes();
  }

  bindLengthCounter('s-problem','s-problem-count',100000);
  bindLengthCounter('s-note','s-note-count',20000);
  bindMistakeListEvents(document.getElementById('mistake-list'));
  document.getElementById('f-search')?.addEventListener('input',scheduleMistakeSearch);
  document.getElementById('f-chapter')?.addEventListener('change',()=>{
    syncListNavigation();void loadMistakes();
  });
  document.getElementById('f-status')?.addEventListener('change',()=>{
    syncListNavigation();void loadMistakes();
  });
  document.getElementById('mistake-load-more')?.addEventListener('click',
    ()=>state.view==='trash'?loadTrash({append:true}):loadMistakes({append:true}));
  document.getElementById('mistake-select-loaded')?.addEventListener('click',
    exportsController.selectAllLoaded);
  document.getElementById('mistake-clear-selection')?.addEventListener('click',
    exportsController.clearSelection);
  document.getElementById('mistake-export-pdf')?.addEventListener('click',
    exportsController.chooseLayout);
  document.getElementById('mistake-bulk-manage')?.addEventListener('click',
    exportsController.openBulk);
  document.getElementById('mistake-export-history')?.addEventListener('click',
    exportsController.openHistory);
  document.getElementById('mistake-view-current')?.addEventListener('click',()=>void setListView('current'));
  document.getElementById('mistake-view-trash')?.addEventListener('click',()=>void setListView('trash'));
  exportsController.updateBar();

  window.Cuoti.mistakes.controller=Object.freeze({
    add,load:loadMistakes,render:renderMistakes,applyRoute:applyListRoute,
    routeParams:getListRouteParams,filterSubject,loadTrash,discardTrashItem,setListView,
    mergeUnique:mergeUniqueMistakes
  });
  const mountAdd=()=>loadSubjects('s');
  const unmountList=()=>{
    state.requestRevision+=1;
    state.requestController?.abort();
  };
  const mountList=({params=new URLSearchParams()}={})=>applyListRoute(params);
  window.Cuoti.features.register('add',Object.freeze({
    mount:mountAdd,
    unmount:()=>{},
    refresh:mountAdd,
    add
  }));
  window.Cuoti.features.register('list',Object.freeze({
    mount:mountList,
    unmount:unmountList,
    refresh:mountList
  }));
  window.getListRouteParams=getListRouteParams;
  window.applyListRoute=applyListRoute;
})();
