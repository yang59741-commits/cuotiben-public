(function initDailyReviewController(){
  'use strict';
  const feature=window.Cuoti.dailyReview;
  const state=feature.state;
  const UNDO_DELAY=5000;
  const PREFS_KEY='cuoti-daily-generation';
  const DRAWER_TEXT=Object.freeze({
    generate:['生成新批次','选题并导出 PDF，生成后自动下载'],
    remediation:['挑选再练','再次答错或提示后答对的题，按到期时间排序'],
    summary:['练习表现','查看独立作答与跨日巩固情况']
  });
  let subjects=[];
  let types=[];
  let methods=[];
  let undoTimer=null;
  const pendingWrites=new Set();
  let remediationDirty=false;
  let drawerPreviousFocus=null;
  let summaryRevision=0;
  let summaryController=null;
  let summaryChapterRevision=0;
  const el=id=>document.getElementById(id);
  const render=()=>feature.render;
  function label(session){return session?.selection?.practice_kind==='remediation'?`复错再练 · 批次 ${safeInteger(session.id)}`:session?.practice_mode==='focused'?`专项第 ${safeInteger(session.round_number)} 批`:`全库第 ${safeInteger(session?.round_number)} 轮 · 批次 ${safeInteger(session?.id)}`;}
  function guarded(task){return async(...args)=>{try{await task(...args)}catch(error){setStatus(error.message)}};}
  function readPrefs(){try{return JSON.parse(localStorage.getItem(PREFS_KEY)||'{}')||{}}catch{return {}}}
  function savePrefs(){
    const value={mode:state.practiceMode,purpose:radioValue('daily-pdf-purpose','self_test'),layout:radioValue('daily-pdf-layout','compact')};
    try{localStorage.setItem(PREFS_KEY,JSON.stringify(value))}catch{/* Preferences are optional. */}
  }
  function radioValue(name,fallback){return document.querySelector(`[name="${name}"]:checked`)?.value||fallback;}
  function setRadio(name,value){const node=[...document.querySelectorAll(`[name="${name}"]`)].find(input=>input.value===value);if(node)node.checked=true;}
  function setOptions(id,values,placeholder){
    const node=el(id);if(!node)return;const previous=node.value;
    node.replaceChildren(...(placeholder?[new Option(placeholder,'')]:[]),...values.map(([value,name])=>new Option(name,String(value))));
    if([...node.options].some(o=>o.value===previous))node.value=previous;
    syncCustomSelect(node);
  }
  function typeName(code){return types.find(type=>type.code===code)?.name||'';}
  function methodName(code){return methods.find(method=>method.code===code)?.name||'';}
  function hasMethod(typed,code,subject){
    if(!['auto','manual'].includes(typed.status))return false;
    if(typed.method_annotations)return typed.method_annotations.some(item=>item.code===code);
    const method=methods.find(item=>item.code===code);
    const key=value=>String(value||'').normalize('NFKC').trim().replace(/\s+/g,' ').toLowerCase();
    return Boolean(method&&method.subjects.includes(subject)&&(typed.methods||[]).some(name=>[method.name,...method.aliases].some(alias=>key(alias)===key(name))));
  }

  /* ---------- derived view state ---------- */
  function outcomeOf(item){
    if(state.queued?.itemId===item.id)return state.queued.choice;
    return render().outcome(item.result,item.assistance);
  }
  function counts(items=state.items){
    const value={total:items.length,pending:0,solo:0,hint:0,correct_unknown:0,again:0,incomplete:0};
    for(const item of items)value[outcomeOf(item)]+=1;
    return value;
  }
  function matchesFilters(item){
    const typed=item.classification||{};
    if(state.subjectId&&String(item.subjectId)!==String(state.subjectId))return false;
    if(state.filters.chapter_id&&String(item.chapterId)!==String(state.filters.chapter_id))return false;
    if(state.filters.primary_type_code&&typed.primary_type_code!==state.filters.primary_type_code)return false;
    if(state.filters.method_code&&!hasMethod(typed,state.filters.method_code,item.subject))return false;
    if(state.filters.classification_status&&(typed.status||'historical_unknown')!==state.filters.classification_status)return false;
    return true;
  }
  function filteredItems(){return state.items.filter(matchesFilters);}
  function visibleItems(){
    const items=filteredItems();
    if(state.resultChip==='all')return items;
    return items.filter(item=>outcomeOf(item)===state.resultChip);
  }
  function hintUsed(item){return window.Cuoti.reviewEvidence?.value(render().evidenceKey(item))==='hint';}
  function viewFor(item){
    return {
      index:state.items.indexOf(item)+1,
      current:state.currentId===item.id,
      hintOpen:state.hintOpen.has(item.id),
      expanded:state.expanded.has(item.id),
      queued:state.queued?.itemId===item.id?state.queued:null,
      hintUsed:hintUsed(item)
    };
  }
  function serverFilterCount(){
    return (state.subjectId?1:0)+Object.entries(state.filters).filter(([key,value])=>key!=='result'&&value!==''&&value!=null).length;
  }
  function pendingOrder(){
    return [...document.querySelectorAll('#daily-review-list .daily-q.is-pending')].map(node=>safeInteger(node.dataset.dailyReviewItem));
  }

  /* ---------- rendering ---------- */
  function renderItems(){
    const root=el('daily-review-list');
    if(!root)return;
    root.innerHTML=render().list(visibleItems(),viewFor,{grouped:state.grouped,hasSession:Boolean(state.session)});
    const order=pendingOrder();
    if(!order.includes(state.currentId))setCurrent(order[0]??null);
    void renderMath(root);
  }
  function cardNode(id){return document.querySelector(`#daily-review-list [data-daily-review-item="${safeInteger(id)}"]`);}
  function renderCard(id){
    const item=state.items.find(candidate=>candidate.id===id);
    const node=cardNode(id);
    if(!item||!node)return;
    const template=document.createElement('template');
    template.innerHTML=render().card(item,viewFor(item)).trim();
    const next=template.content.firstElementChild;
    node.replaceWith(next);
    void renderMath(next);
  }
  function renderChrome(){
    const value=counts();
    const stats=el('daily-review-stats');
    if(stats){
      stats.innerHTML=state.session?render().progress(value):'';
      stats.hidden=!state.session;
    }
    const chips=el('daily-result-chips');
    if(chips)chips.innerHTML=state.session?render().chips(counts(filteredItems()),state.resultChip):'';
    const finish=el('daily-finish');
    if(finish)finish.innerHTML=state.session&&value.total&&!value.pending?render().finish(value):'';
    const filterCount=el('daily-filter-count');
    if(filterCount){const active=serverFilterCount();filterCount.hidden=!active;filterCount.textContent=String(active);}
    const title=el('daily-batch-title');
    // Without a batch the empty state below carries the message and the call to action.
    if(title)title.textContent=state.session?label(state.session):'每日自主复习';
    const generate=el('daily-open-generate');
    if(generate)generate.hidden=!state.session;
    const meta=el('daily-batch-meta');
    if(meta)meta.innerHTML=render().sessionMeta(state.session,methodName(state.session?.selection?.method_code)||typeName(state.session?.selection?.primary_type_code));
    const listBar=document.querySelector('#review-daily-panel .daily-list-bar');
    if(listBar)listBar.hidden=!state.session;
    const kicker=document.querySelector('#review-daily-panel .daily-batch-kicker');
    if(kicker)kicker.hidden=!state.session;
    const shortcuts=el('daily-shortcuts');
    if(shortcuts)shortcuts.hidden=!value.pending;
  }
  function setCurrent(id,{scroll=false,focus=false}={}){
    state.currentId=id??null;
    document.querySelectorAll('#daily-review-list .daily-q.is-pending').forEach(node=>{
      node.classList.toggle('is-current',safeInteger(node.dataset.dailyReviewItem)===state.currentId);
    });
    const node=state.currentId?cardNode(state.currentId):null;
    if(node&&scroll)node.scrollIntoView({behavior:window.matchMedia?.('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});
    if(node&&focus)node.focus({preventScroll:true});
  }
  function setStatus(text,{visible=true}={}){
    const node=el('daily-review-status');
    if(node)node.textContent=String(text||'');
    const toolbar=el('daily-review-export-status');
    if(toolbar)toolbar.textContent=visible?String(text||''):'';
  }

  /* ---------- recording with undo ---------- */
  function choose(itemId,choice){
    const item=state.items.find(candidate=>candidate.id===safeInteger(itemId));
    const roundId=safeInteger(state.session?.round_id??state.session?.id);
    if(!item||!roundId||item.readOnly||item.result||state.pendingIds.has(item.id)||!render().CHOICES.includes(choice))return;
    if(state.queued?.itemId===item.id)return;
    void flushQueued();
    const order=pendingOrder();
    const position=order.indexOf(item.id);
    const next=[...order.slice(position+1),...order.slice(0,Math.max(0,position))].find(id=>id!==item.id)??null;
    const assistance=choice==='solo'?'none':choice==='hint'?'hint':(hintUsed(item)?'hint':'none');
    state.queued={itemId:item.id,roundId,choice,result:choice==='solo'||choice==='hint'?'correct':choice,assistance};
    state.hintOpen.delete(item.id);
    renderCard(item.id);
    renderChrome();
    setCurrent(next,{scroll:true,focus:true});
    showUndo(item,choice);
    clearTimeout(undoTimer);
    undoTimer=setTimeout(()=>{void flushQueued()},UNDO_DELAY);
  }
  function showUndo(item,choice){
    const root=el('daily-undo');
    if(!root)return;
    el('daily-undo-message').innerHTML=`第 ${state.items.indexOf(item)+1} 题 · <b>${escapeHTML(render().outcomeLabel(choice))}</b>`;
    const bar=el('daily-undo-progress');
    if(bar){bar.style.animation='none';void bar.offsetWidth;bar.style.animation='';}
    root.hidden=false;
  }
  function hideUndo(){const root=el('daily-undo');if(root)root.hidden=true;}
  function undo(){
    const queued=state.queued;
    if(!queued)return;
    clearTimeout(undoTimer);
    state.queued=null;
    hideUndo();
    renderCard(queued.itemId);
    renderChrome();
    setCurrent(queued.itemId,{scroll:true,focus:true});
    setStatus('已撤销，这道题仍待记录',{visible:false});
  }
  async function flushQueued(){
    const mountRevision=state.mountRevision;
    const queued=state.queued;
    if(queued){
      clearTimeout(undoTimer);
      state.queued=null;
      hideUndo();
      const pending=submitRecord(queued);
      pendingWrites.add(pending);
      pending.then(()=>pendingWrites.delete(pending),()=>pendingWrites.delete(pending));
    }
    // Include writes started by the undo timer or another card, not just the
    // queued card. A refreshed session must not replace their optimistic state.
    while(pendingWrites.size)await Promise.all([...pendingWrites]);
    if(remediationDirty&&mountRevision===state.mountRevision){
      remediationDirty=false;
      const revision=state.requestRevision;
      void remediation(revision).catch(error=>{
        if(revision===state.requestRevision)setStatus('作答已保存，待巩固列表暂不可用：'+error.message);
      });
    }
  }
  async function submitRecord(queued){
    const item=state.items.find(candidate=>candidate.id===queued.itemId);
    if(!item||item.readOnly||state.pendingIds.has(item.id))return;
    state.requestRevision+=1;
    state.requestController?.abort();
    state.pendingIds.add(item.id);item.pending=true;
    const previous={result:item.result,assistance:item.assistance};
    item.result=queued.result;item.assistance=queued.assistance;
    const key=globalThis.crypto?.randomUUID?.()||`${Date.now()}-${item.id}`;
    try{
      const response=await feature.api.submit(queued.roundId,item.id,queued.result,key,queued.assistance);
      item.result=response?.completion_result||response?.result||queued.result;
      item.assistance=response?.assistance||queued.assistance;
      setStatus(`已记录：${render().outcomeLabel(render().outcome(item.result,item.assistance))}`,{visible:false});
      remediationDirty=true;
    }catch(error){
      if(error?.status===409&&error?.data?.code==='DAILY_REVIEW_CONFLICT'){
        // Reload after this write settles; awaiting it here would wait on self.
        void load();showModal('结果已记录',error.message,'info');
      }
      else{item.result=previous.result;item.assistance=previous.assistance;showModal('记录每日复习结果失败',error.message,'error');}
    }finally{
      state.pendingIds.delete(item.id);item.pending=false;
      if(state.items.includes(item)){renderCard(item.id);renderChrome();}
    }
  }

  /* ---------- hints / expand ---------- */
  function toggleHint(itemId){
    const id=safeInteger(itemId);
    const item=state.items.find(candidate=>candidate.id===id);
    const node=cardNode(id);
    if(!item||!node)return;
    const open=!state.hintOpen.has(id);
    if(open)state.hintOpen.add(id);else state.hintOpen.delete(id);
    node.querySelector('.daily-hint-panel')?.toggleAttribute('hidden',!open);
    node.querySelector('[data-daily-hint]')?.setAttribute('aria-expanded',String(open));
    if(open&&!item.result&&state.queued?.itemId!==id){
      window.Cuoti.reviewEvidence?.set(render().evidenceKey(item),'hint');
      node.querySelector('.daily-rec-btn[data-result="hint"]')?.classList.add('is-suggested');
      node.querySelector('.daily-rec-btn[data-result="solo"]')?.classList.add('is-muted');
      const note=node.querySelector('.daily-viewed-note');if(note)note.hidden=false;
    }
    if(!item.result)setCurrent(id);
  }
  function toggleExpand(itemId){
    const id=safeInteger(itemId);
    if(state.queued?.itemId===id)return;
    if(state.expanded.has(id))state.expanded.delete(id);else state.expanded.add(id);
    renderCard(id);
    cardNode(id)?.querySelector('[data-daily-expand]')?.focus();
  }
  function move(step){
    const order=pendingOrder().filter(id=>id!==state.queued?.itemId);
    if(!order.length)return;
    const index=order.indexOf(state.currentId);
    const next=index<0?order[0]:order[(index+step+order.length)%order.length];
    setCurrent(next,{scroll:true,focus:true});
  }

  /* ---------- drawer ---------- */
  function drawerOpen(){return !el('daily-drawer')?.hidden;}
  function setBackgroundInert(inert){
    document.querySelectorAll('.main,.sb').forEach(element=>{
      if(inert)element.setAttribute('inert','');
      else element.removeAttribute('inert');
    });
  }
  function renderDrawer(view){
    const drawer=el('daily-drawer'),scrim=el('daily-drawer-scrim');
    if(!drawer||!scrim)return;
    const open=Boolean(view);
    const wasOpen=!drawer.hidden;
    if(open&&!wasOpen)drawerPreviousFocus=document.activeElement;
    state.drawer=open?view:null;
    if(view!=='summary'){summaryRevision+=1;summaryChapterRevision+=1;summaryController?.abort();}
    drawer.querySelectorAll('[data-drawer-view]').forEach(section=>{section.hidden=section.dataset.drawerView!==view});
    drawer.querySelectorAll('[data-drawer-footer]').forEach(footer=>{footer.hidden=footer.dataset.drawerFooter!==view});
    if(open){
      const [title,subtitle]=DRAWER_TEXT[view]||DRAWER_TEXT.generate;
      el('daily-drawer-title').textContent=title;
      el('daily-drawer-subtitle').textContent=subtitle;
    }
    drawer.hidden=!open;scrim.hidden=!open;
    setBackgroundInert(open);
    if(open){if(!wasOpen)el('daily-drawer-close')?.focus();}
    else if(wasOpen){
      let restore=drawerPreviousFocus;drawerPreviousFocus=null;
      if(restore?.closest?.('#daily-more-menu'))restore=el('daily-more-toggle');
      if(!restore?.isConnected||!restore.getClientRects().length)restore=el('daily-open-generate');
      restore?.focus?.({preventScroll:true});
    }
  }
  function openDrawer(view){
    closeMenu();
    const wasOpen=drawerOpen();
    renderDrawer(view);
    if(view==='generate')prepareGenerate();
    if(view==='remediation')renderRemediationDrawer();
    if(view==='summary')void summary();
    if(!wasOpen)window.pushHistoryLayer?.('daily-drawer',()=>renderDrawer(false));
  }
  function closeDrawer(){
    if(!drawerOpen())return;
    if(!window.dismissHistoryLayer?.('daily-drawer'))renderDrawer(false);
  }
  function prepareGenerate(){
    const warning=el('daily-generate-warning');
    const pending=counts().pending;
    if(warning){
      warning.hidden=!(state.session&&pending);
      warning.textContent=pending?`当前批次还有 ${pending} 道未记录。生成新批次后，仍可在「批次历史」里补记。`:'';
    }
    syncModeFields();
  }
  function syncModeFields(){
    const focused=state.practiceMode!=='mixed';
    const method=state.practiceMode==='method';
    el('daily-focused-options').hidden=!focused;
    el('daily-focus-type-field').hidden=method;
    el('daily-focus-method-field').hidden=!method;
    el('daily-focus-method-role-field').hidden=!method;
    const help=el('daily-mode-help');
    if(help)help.textContent=method?'集中练需要同一解题方法的题目，可限定主要方法或包含辅助方法。':focused?'集中练同一题型，适合刚发现的薄弱点。':'从全库按轮次抽取未删除的错题，包含已掌握题目。';
  }
  function trapDrawerFocus(event){
    const drawer=el('daily-drawer');
    const focusable=[...drawer.querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])')].filter(node=>!node.closest('[hidden]')&&node.getClientRects().length);
    if(!focusable.length){event.preventDefault();drawer.focus();return}
    const first=focusable[0],last=focusable[focusable.length-1];
    if(!drawer.contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first).focus();}
    else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }

  /* ---------- more menu ---------- */
  function menuOpen(){return !el('daily-more-menu')?.hidden;}
  function openMenu(){
    const menu=el('daily-more-menu');if(!menu)return;
    menu.hidden=false;el('daily-more-toggle').setAttribute('aria-expanded','true');
    menu.querySelector('[role="menuitem"]')?.focus();
  }
  function closeMenu({restore=false}={}){
    const menu=el('daily-more-menu');if(!menu||menu.hidden)return;
    menu.hidden=true;el('daily-more-toggle').setAttribute('aria-expanded','false');
    if(restore)el('daily-more-toggle').focus();
  }

  /* ---------- data loading ---------- */
  async function loadTypes(){
    const [typeData,methodData]=await Promise.all([api('/api/v2/question-types'),api('/api/v2/solution-methods')]);
    types=typeData.items;methods=methodData.items;await viewOptions();await focusOptions();renderChrome();
  }
  async function viewOptions(){
    const selected=state.subjectId;
    const subject=subjects.find(s=>String(s.id)===selected);
    const chapters=selected?await api('/api/chapters?subject_id='+selected):[];
    if(selected!==state.subjectId)return;
    setOptions('daily-view-chapter',chapters.map(c=>[c.id,c.name]),'全部章节');
    setOptions('daily-view-type',types.filter(t=>!subject||t.subject===subject.name).map(t=>[t.code,(subject?'':t.subject+' · ')+t.name]),'全部题型');
    setOptions('daily-view-method',methods.filter(item=>!subject||item.subjects.includes(subject.name)).map(item=>[item.code,item.name]),'全部方法');
  }
  async function focusOptions(){
    const id=el('daily-focus-subject')?.value;
    const subject=subjects.find(s=>String(s.id)===id);
    const chapters=id?await api('/api/chapters?subject_id='+id):[];
    if(id!==el('daily-focus-subject')?.value)return;
    setOptions('daily-focus-type',types.filter(t=>t.subject===subject?.name).map(t=>[t.code,t.name]),'请选择题型');
    setOptions('daily-focus-method',methods.filter(item=>item.subjects.includes(subject?.name)).map(item=>[item.code,item.name]),'请选择方法');
    updateMethodDefinition();
    setOptions('daily-focus-chapter',chapters.map(c=>[c.id,c.name]),'全部章节');
  }
  function updateMethodDefinition(){el('daily-focus-method-definition').textContent=methods.find(item=>item.code===el('daily-focus-method').value)?.definition||'';}
  async function summary(){
    const root=el('daily-type-summary-content');
    if(!root)return;
    const revision=++summaryRevision;
    summaryController?.abort();
    summaryController=new AbortController();
    const method=radioValue('daily-summary-dimension','type')==='method';
    const filters=el('daily-summary-filters');
    if(filters)filters.hidden=!method;
    setOptions('daily-summary-subject',subjects.map(subject=>[subject.id,subject.name]),'全部科目');
    const batch=radioValue('daily-summary-scope','batch')==='batch';
    const id=state.session?.id;
    const subject=subjects.find(item=>String(item.id)===el('daily-summary-subject')?.value);
    el('daily-drawer-subtitle').textContent=(batch?'本批':'近 30 天')+(method?' · '+(subject?.name||'全部科目'):' · 题型表现');
    if(batch&&!id){root.innerHTML='<p class="daily-drawer-help">尚无复习批次。</p>';return;}
    root.innerHTML=`<p class="daily-summary-loading" role="status">正在读取${method?'方法':'题型'}表现…</p>`;
    const params=new URLSearchParams();
    if(batch)params.set('session_id',id);
    if(method){
      if(el('daily-summary-subject')?.value)params.set('subject_id',el('daily-summary-subject').value);
      if(el('daily-summary-chapter')?.value)params.set('chapter_id',el('daily-summary-chapter').value);
    }
    try{
      const result=await api(`/api/v2/daily-review/${method?'method':'type'}-summary?${params}`,{signal:summaryController.signal});
      if(revision!==summaryRevision||state.drawer!=='summary'||(batch&&id!==state.session?.id))return;
      root.innerHTML=method?render().methodSummary(result):render().summaryTable(result.items||[]);
    }catch(error){
      if(error.name!=='AbortError'&&revision===summaryRevision&&state.drawer==='summary')root.innerHTML=`<div class="daily-summary-error" role="alert"><strong>${icon('circleAlert')}统计读取失败</strong><span>${escapeHTML(error.message)}，筛选条件已保留。</span><button type="button" class="btn btn-out btn-sm daily-summary-retry">重试</button></div>`;
    }
  }
  async function summaryChapters(){
    const revision=++summaryChapterRevision;
    const id=el('daily-summary-subject').value;
    el('daily-summary-chapter').value='';
    el('daily-summary-chapter').disabled=true;
    setOptions('daily-summary-chapter',[],id?'正在读取章节…':'请先选择科目');
    void summary();
    try{
      const chapters=id?await api('/api/chapters?subject_id='+id):[];
      if(revision===summaryChapterRevision&&state.drawer==='summary'){
        el('daily-summary-chapter').disabled=!id;
        setOptions('daily-summary-chapter',chapters.map(chapter=>[chapter.id,chapter.name]),id?'全部章节':'请先选择科目');
      }
    }catch(error){
      if(revision===summaryChapterRevision&&state.drawer==='summary')el('daily-type-summary-content').textContent='章节读取失败：'+error.message;
    }
  }
  async function remediation(revision=state.requestRevision){
    const payload=await api('/api/v2/daily-review/remediation');
    if(revision!==state.requestRevision)return;
    const items=(payload.items||[]).filter(item=>(!state.subjectId||String(item.subject_id)===state.subjectId)&&(!state.filters.chapter_id||String(item.chapter_id)===String(state.filters.chapter_id)));
    state.remediationItems=items;
    const banner=el('daily-remediation'),count=el('daily-remediation-count');
    const due=items.filter(item=>item.due).length;
    if(count)count.textContent=`${items.length} 道${due?`，今天到期 ${due} 道`:''}`;
    if(banner)banner.hidden=!items.length;
    if(state.drawer==='remediation')renderRemediationDrawer();
  }
  function renderRemediationDrawer(){
    const root=el('daily-remediation-content');
    if(!root)return;
    root.innerHTML=render().remediationRows(state.remediationItems);
    updateRemediationSelection();
  }
  function updateRemediationSelection(){
    const root=el('daily-remediation-content');
    const selected=root?root.querySelectorAll('[data-remediation-id]:checked').length:0;
    const note=el('daily-remediation-selected');
    if(note){note.textContent=state.remediationItems.length?`已选 ${selected} / 10`:'';note.classList.toggle('is-over',selected>10);}
    const button=el('daily-remediation-generate');
    if(button)button.disabled=!selected||selected>10||state.generationInFlight;
  }
  function reopenRemediation(publicId){
    const item=state.remediationItems.find(row=>row.public_id===publicId);
    if(!item)return;
    showModal('重新开启间隔复习','该题将改为未掌握，并于 3 天后开始间隔复习。','info','<button type="button" class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button type="button" class="btn btn-p btn-sm" id="confirm-remediation-reopen">确认重开</button>');
    el('confirm-remediation-reopen').addEventListener('click',guarded(async()=>{
      el('confirm-remediation-reopen').disabled=true;
      try{await api('/api/v2/mistakes/'+encodeURIComponent(item.public_id)+'/reopen',{method:'POST',body:JSON.stringify({revision:item.revision,client_event_id:crypto.randomUUID()})});hideModal();await remediation();void window.Cuoti.reviewBadge?.refresh?.()}
      catch(error){showModal('重开未完成',error.message,'error');await remediation()}
    }));
  }

  async function history(){
    await flushQueued();
    showModal('复习批次历史','','info');
    const root=document.getElementById('modal-body');
    const layout=reexportLayout(radioValue('daily-pdf-layout','compact'));
    root.innerHTML=`<div class="review-field"><label for="daily-history-layout">重新生成 PDF 的版式</label><select id="daily-history-layout" class="sel-sm">${REEXPORT_LAYOUTS.map(([value,name])=>`<option value="${value}"${value===layout?' selected':''}>${name}</option>`).join('')}</select></div><div id="daily-history-list"></div><button type="button" class="btn btn-out btn-sm" id="daily-history-more">加载更多</button>`;
    let cursor='';
    const more=root.querySelector('#daily-history-more');
    const next=async()=>{
      more.disabled=true;
      try{
        const result=await api('/api/v2/daily-review/sessions?limit=20'+(cursor?'&after='+encodeURIComponent(cursor):''));
        if(!root.querySelector('#daily-history-list'))return;
        const list=root.querySelector('#daily-history-list');
        const currentId=safeInteger(state.session?.id);
        list.insertAdjacentHTML('beforeend',result.items.map(s=>{
          const done=safeInteger(s.completed_count),total=safeInteger(s.item_count);
          // The open-session row and the PDF action are siblings: no nested buttons.
          return `<div class="review-history-entry"><button type="button" class="review-history-row${safeInteger(s.id)===currentId?' is-current':''}" data-session="${safeInteger(s.id)}"><strong>${escapeHTML(label(s))}</strong><span>${done} / ${total} 已记录</span><span class="review-history-bar" aria-hidden="true"><i style="flex-grow:${done}"></i><i style="flex-grow:${Math.max(0,total-done)}"></i></span><time>${escapeHTML(new Date(s.created_at).toLocaleString('zh-CN'))}</time></button>${reexportButtonHTML(s.id,label(s))}</div>`;
        }).join('')||'<p>暂无历史批次。</p>');
        cursor=result.next_cursor;more.hidden=!result.has_more;
      }finally{more.disabled=false}
    };
    root.querySelector('#daily-history-list').addEventListener('click',event=>{
      const pdf=event.target.closest('[data-session-pdf]');
      if(pdf){
        // Never let the export action bubble into the row's open-session action.
        event.preventDefault();event.stopPropagation();
        if(!pdf.disabled)void reexportPdf(pdf.dataset.sessionPdf,root.querySelector('#daily-history-layout')?.value,pdf.dataset.sessionLabel);
        return;
      }
      const row=event.target.closest('[data-session]');
      if(row)void guarded(async()=>{state.sessionId=Number(row.dataset.session);hideModal();await load()})();
    });
    more.addEventListener('click',guarded(next));await next();
  }

  /* ---------- historical PDF re-export ---------- */
  const REEXPORT_LAYOUTS=Object.freeze([['compact','紧凑'],['notes','笔记留白'],['two_column','双栏']]);
  function reexportLayout(value){return REEXPORT_LAYOUTS.some(([code])=>code===value)?value:'compact';}
  function reexportButtonContent(busy){return busy?icon('loader','icon-spin')+'<span>生成中…</span>':icon('download')+'<span>重新生成 PDF</span>';}
  function reexportButtonHTML(sessionId,name=''){
    const id=safeInteger(sessionId);
    const busy=state.reexports.has(id);
    return `<button type="button" class="btn btn-txt btn-sm" data-session-pdf="${id}" data-session-label="${escapeHTML(name)}" aria-label="${escapeHTML(`重新生成 ${name||'批次 '+id} 的 PDF`)}"${busy?' disabled aria-busy="true"':''}>${reexportButtonContent(busy)}</button>`;
  }
  function syncReexportButtons(){
    document.querySelectorAll('[data-session-pdf]').forEach(button=>{
      const busy=state.reexports.has(safeInteger(button.dataset.sessionPdf));
      button.disabled=busy;
      if(busy)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
      button.innerHTML=reexportButtonContent(busy);
    });
  }
  const REEXPORT_CODE_HINTS=Object.freeze({
    ROUND_NOT_FOUND:'该批次不存在，可能已被删除。',
    DAILY_REVIEW_CONFLICT:'只有已发布或已完成的批次可以重新生成 PDF。',
    DAILY_REVIEW_SNAPSHOT_UNAVAILABLE:'该批次保存的题目快照不完整，无法按原题重新生成；不会改用当前题目内容，也不会另选新题。',
    IDEMPOTENCY_CONFLICT:'请求标识与另一项导出冲突，请重新点击。'
  });
  function reexportFailure(error,uncertain){
    const code=String(error?.data?.code||error?.code||'').toUpperCase();
    const hint=REEXPORT_CODE_HINTS[code]||'';
    const detail=[error?.message,hint].filter(Boolean).join(' ');
    if(error?.name==='JobError'){
      const status=String(error.job?.status||'');
      if(status==='cancelled')return ['PDF 重新生成已取消','导出任务已取消，没有生成新的 PDF。当前批次与作答记录不受影响。'];
      if(status==='interrupted')return ['PDF 重新生成中断',`${detail} 可在任务面板中重试，或再次点击重新生成。`];
      if(hint)return ['PDF 重新生成失败',detail];
      return ['PDF 编译失败',`${detail} 当前批次与作答记录不受影响。`];
    }
    if(uncertain)return ['PDF 重新生成未确认',`${detail} 再次点击同一批次和版式会沿用本次请求标识，不会重复生成。`];
    return ['PDF 重新生成失败',detail];
  }
  // Rebuild one historical session's PDF from its saved snapshots. The target
  // is captured by the caller at click time; nothing here reads or changes the
  // displayed session, filters, queued results or the new-batch flow.
  async function reexportPdf(sessionId,layoutValue,name=''){
    const id=safeInteger(sessionId);
    const layout=reexportLayout(layoutValue);
    // Synchronous duplicate guard, before any await.
    if(!id||state.reexports.has(id))return null;
    const identity=`${id}|${layout}`;
    const key=state.reexportKeys.get(identity)||crypto.randomUUID();
    state.reexportKeys.set(identity,key);
    state.reexports.set(id,{sessionId:id,layout,key});
    syncReexportButtons();
    const title=name||`批次 ${id}`;
    setStatus(`正在重新生成 ${title} 的 PDF…`);
    let uncertain=true;
    try{
      let submitted;
      try{
        submitted=await api(`/api/v2/daily-review/sessions/${encodeURIComponent(id)}/pdf`,{
          method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify({layout})
        });
      }catch(error){
        // A 4xx is a definitive rejection; network errors and 5xx may have
        // enqueued the job, so the same key is kept for an explicit retry.
        uncertain=!(error?.status>=400&&error.status<500);
        throw error;
      }
      const tracked=window.trackJob(submitted);
      if(!tracked?.id)throw new Error('服务器没有返回导出任务。');
      uncertain=false;
      const completed=await window.subscribeJob(tracked.id);
      let result=completed.result;
      if(result==null){
        // The job finished; replaying the same key only re-reads this job.
        uncertain=true;
        result=(await api(`/api/v2/jobs/${encodeURIComponent(tracked.id)}/result`))?.result;
        uncertain=false;
      }
      if(!safeLocalURL(result?.download_url))throw new Error('导出结果缺少下载地址。');
      state.reexportKeys.delete(identity);
      download(result);
      const count=safeInteger(result?.item_count);
      setStatus(`已重新生成 ${title} 的 PDF${count?`（${count} 道题）`:''}，已开始下载`);
      if(result?.legacy_live_content)showToast('PDF 已重新生成：该批次的题目快照是在早期数据迁移时保存的。新 PDF 按保留的历史快照生成，可能与最初导出的 PDF 不完全一致。','info',{persistent:true});
      return result;
    }catch(error){
      if(!uncertain)state.reexportKeys.delete(identity);
      const [heading,message]=reexportFailure(error,uncertain);
      setStatus(heading);
      // Completion can arrive after the user opens another editor or dialog.
      showToast(`${heading}：${message}`,'error',{persistent:true});
      return null;
    }finally{
      state.reexports.delete(id);
      syncReexportButtons();
    }
  }

  function applySession(payload){
    const sessionId=safeInteger(payload?.session_id??payload?.round_id??payload?.id);
    const previousId=safeInteger(state.session?.id);
    state.session=sessionId&&Array.isArray(payload?.items)?payload:null;
    if(safeInteger(state.session?.id)!==previousId){state.hintOpen.clear();state.expanded.clear();state.currentId=null;state.resultChip='all';}
    state.items=(state.session?.items||[]).map(render().normalize);
    for(const item of state.items){if(!item.result)window.Cuoti.reviewEvidence?.initialize(render().evidenceKey(item),state.session?.selection?.pdf_purpose==='guided'?'hint':'none')}
    if(state.resultChip!=='all'&&!filteredItems().some(item=>outcomeOf(item)===state.resultChip))state.resultChip='all';
    renderView();
  }
  function renderView(){
    renderChrome();
    renderItems();
    setStatus(state.session?`${label(state.session)} · 本批 ${state.items.length} 道 · 当前显示 ${visibleItems().length} 道`:'尚未生成每日自主复习',{visible:false});
  }

  async function load(){
    const mountRevision=state.mountRevision;
    await flushQueued();
    if(mountRevision!==state.mountRevision)return;
    const revision=++state.requestRevision;
    state.requestController?.abort();
    state.requestController=new AbortController();
    setStatus('正在读取每日自主复习…',{visible:false});
    try{
      const payload=await feature.api.current('',state.requestController.signal,{},state.sessionId);
      if(revision!==state.requestRevision)return;
      applySession(payload);
      setStatus('',{visible:true});
      await remediation(revision).catch(error=>setStatus('待巩固列表暂不可用：'+error.message));
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.requestRevision)return;
      applySession(null);setStatus('每日自主复习读取失败：'+error.message);
    }
  }

  function download(result){
    const href=safeLocalURL(result?.download_url);
    if(!href)return;
    const link=document.createElement('a');link.href=href;link.download=String(result?.download_name||'每日自主复习批次.zip');link.hidden=true;document.body.appendChild(link);link.click();link.remove();
  }

  function setGenerating(active){
    const buttons=[el('daily-review-generate-pdf'),el('daily-remediation-generate')];
    buttons.forEach(button=>{
      if(!button)return;
      if(active){button.dataset.label=button.dataset.label||button.innerHTML;button.disabled=true;button.innerHTML=icon('loader','icon-spin')+'<span>生成中…</span>';}
      else{if(button.dataset.label)button.innerHTML=button.dataset.label;button.disabled=false;}
    });
    const open=el('daily-open-generate');if(open)open.disabled=active;
    if(!active)updateRemediationSelection();
  }

  async function generate(override=null){
    if(state.generationInFlight)return;
    state.generationInFlight=true;
    setGenerating(true);
    setStatus('正在生成每日自主复习 PDF…');
    try{
      await flushQueued();
      const selection=override||(state.practiceMode!=='mixed'?{
        practice_mode:'focused',subject_id:el('daily-focus-subject').value,
        ...(state.practiceMode==='method'?{method_code:el('daily-focus-method').value,method_role:el('daily-focus-method-role').value}:{primary_type_code:el('daily-focus-type').value}),
        chapter_id:el('daily-focus-chapter').value,count:Number(el('daily-focus-count').value),include_mastered:el('daily-focus-mastered').checked
      }:{practice_mode:'mixed'});
      selection.pdf_purpose=radioValue('daily-pdf-purpose','self_test');
      const layout=radioValue('daily-pdf-layout','compact');
      const identity=JSON.stringify([selection,layout]);
      if(state.generationRequest?.identity!==identity)state.generationRequest={identity,id:crypto.randomUUID()};
      const result=await runJobRequest('mistake-export',{
        export_context:'daily_review',
        layout,selection,
        daily_review_request_id:state.generationRequest.id
      });
      state.lastExport=result;download(result);state.generationRequest=null;state.sessionId=null;
      closeDrawer();
      await load();
      setStatus(`已生成 ${label(state.session)} · ${safeInteger(result?.item_count)} 道题，PDF 已开始下载`);
    }catch(error){
      showModal('每日自主复习生成失败',error.message,'error');setStatus('生成失败');
    }finally{
      state.generationInFlight=false;
      setGenerating(false);
    }
  }

  function optionExists(select,value){
    return [...(select?.options||[])].some(option=>option.value===String(value));
  }

  async function initToolbar(){
    if(state.toolbarPromise)return state.toolbarPromise;
    state.toolbarPromise=(async()=>{
      const select=document.getElementById('daily-review-subject');
      try{
        subjects=await feature.api.subjects();
        for(const subject of subjects){
          select?.add(new Option(String(subject.name||''),String(safeInteger(subject.id,''))));
        }
        if(select)syncCustomSelect(select);
        setOptions('daily-focus-subject',subjects.map(s=>[s.id,s.name]));
        await loadTypes();
      }catch(error){
        if(select){select.disabled=true;syncCustomSelect(select)}
        setStatus('科目筛选暂不可用，可继续查看全部轮次题目');
      }
      select?.addEventListener('change',()=>{
        state.subjectId=select.value;
        delete state.filters.chapter_id;delete state.filters.primary_type_code;delete state.filters.method_code;
        el('daily-view-chapter').value='';el('daily-view-type').value='';el('daily-view-method').value='';
        window.Cuoti.reviewPage?.navigate?.('daily',{replace:true});
      });
      state.toolbarInitialized=true;
    })();
    return state.toolbarPromise;
  }

  async function prefillFocused(type){
    const kind=types.find(item=>item.code===type);
    const subject=kind&&subjects.find(item=>item.name===kind.subject);
    if(!subject)return false;
    el('daily-focus-subject').value=String(subject.id);syncCustomSelect(el('daily-focus-subject'));
    await focusOptions();
    el('daily-focus-type').value=type;syncCustomSelect(el('daily-focus-type'));
    state.practiceMode='focused';setRadio('daily-practice-mode','focused');syncModeFields();
    return true;
  }

  async function prefillMethod(code,subjectId){
    const method=methods.find(item=>item.code===code);
    const subject=subjects.find(item=>String(item.id)===String(subjectId));
    if(!method||!subject||!method.subjects.includes(subject.name))return false;
    el('daily-focus-subject').value=String(subject.id);syncCustomSelect(el('daily-focus-subject'));
    await focusOptions();
    el('daily-focus-method').value=code;syncCustomSelect(el('daily-focus-method'));
    el('daily-focus-method-role').value='primary';syncCustomSelect(el('daily-focus-method-role'));
    el('daily-focus-chapter').value='';syncCustomSelect(el('daily-focus-chapter'));
    updateMethodDefinition();
    state.practiceMode='method';setRadio('daily-practice-mode','method');syncModeFields();
    return true;
  }

  async function applyRouteFilter(params){
    await initToolbar();
    const select=document.getElementById('daily-review-subject');
    const requested=String(params?.get?.('subject_id')||'');
    state.subjectId=optionExists(select,requested)?requested:'';
    if(select){select.value=state.subjectId;syncCustomSelect(select)}
    await viewOptions();
    const chapter=params?.get?.('chapter_id');
    if(chapter&&optionExists(el('daily-view-chapter'),chapter)){state.filters.chapter_id=chapter;el('daily-view-chapter').value=chapter;syncCustomSelect(el('daily-view-chapter'))}
    const type=params?.get?.('primary_type_code');
    if(type&&types.some(item=>item.code===type)&&await prefillFocused(type))return 'generate';
    const method=params?.get?.('method_code');
    if(method&&await prefillMethod(method,requested))return 'generate';
    if(params?.get?.('remediation')==='1')return 'remediation';
    return null;
  }

  function isTypingTarget(target){
    return Boolean(target?.closest?.('input,textarea,select,[contenteditable="true"],.select-control,.modal-overlay'));
  }
  function onKeydown(event){
    if(event.defaultPrevented)return;
    const panel=el('review-daily-panel');
    if(!panel||panel.hidden||!el('p-review')?.classList.contains('on'))return;
    if(!document.getElementById('modal')?.classList.contains('hidden'))return;
    if(drawerOpen()){
      if(event.key==='Escape'){event.preventDefault();closeDrawer();return}
      if(event.key==='Tab')trapDrawerFocus(event);
      return;
    }
    if(menuOpen()){
      if(event.key==='Escape'){event.preventDefault();closeMenu({restore:true});return}
      const items=[...el('daily-more-menu').querySelectorAll('[role="menuitem"]')];
      const index=items.indexOf(document.activeElement);
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();const step=event.key==='ArrowDown'?1:-1;items[(index+step+items.length)%items.length]?.focus();}
      if(event.key==='Tab')closeMenu();
      return;
    }
    if(event.ctrlKey||event.metaKey||event.altKey||isTypingTarget(event.target))return;
    if(el('task-panel')&&!el('task-panel').hidden)return;
    const key=event.key.toLowerCase();
    if(/^[1-4]$/.test(key)){
      if(!state.currentId)return;
      event.preventDefault();choose(state.currentId,render().CHOICES[Number(key)-1]);
    }else if(key==='h'){if(state.currentId){event.preventDefault();toggleHint(state.currentId)}}
    else if(key==='j'){event.preventDefault();move(1)}
    else if(key==='k'){event.preventDefault();move(-1)}
    else if(key==='z'){if(state.queued){event.preventDefault();undo()}}
  }

  function relocate(id){const node=el(id);if(node&&node.parentElement!==document.body)document.body.appendChild(node);}

  function bind(){
    const root=document.getElementById('daily-review-list');
    if(!root||root.dataset.dailyReviewBound==='true')return;
    root.dataset.dailyReviewBound='true';
    ['daily-drawer','daily-drawer-scrim','daily-undo'].forEach(relocate);
    const prefs=readPrefs();
    if(['focused','mixed','method'].includes(prefs.mode)){state.practiceMode=prefs.mode;setRadio('daily-practice-mode',prefs.mode)}
    if(prefs.purpose)setRadio('daily-pdf-purpose',prefs.purpose);
    if(prefs.layout)setRadio('daily-pdf-layout',prefs.layout);
    syncModeFields();

    root.addEventListener('click',event=>{
      const target=event.target;
      const record=target.closest('.js-daily-review-result');
      if(record){if(!record.disabled)choose(record.dataset.itemId,record.dataset.result);return}
      const hint=target.closest('[data-daily-hint]');
      if(hint){toggleHint(hint.dataset.dailyHint);return}
      const expand=target.closest('[data-daily-expand]');
      if(expand){toggleExpand(expand.dataset.dailyExpand);return}
      const pick=target.closest('[data-daily-pick]');
      if(pick){setCurrent(safeInteger(pick.dataset.dailyPick),{focus:true});return}
      const open=target.closest('[data-daily-open]');
      if(open){openDrawer(open.dataset.dailyOpen);return}
      if(target.closest('button,a,input,label,select,.select-control'))return;
      const card=target.closest('.daily-q.is-pending');
      if(card)setCurrent(safeInteger(card.dataset.dailyReviewItem));
    });
    el('daily-finish')?.addEventListener('click',event=>{const open=event.target.closest('[data-daily-open]');if(open)openDrawer(open.dataset.dailyOpen)});
    el('daily-result-chips')?.addEventListener('click',event=>{
      const chip=event.target.closest('[data-daily-chip]');
      if(!chip||chip.disabled)return;
      state.resultChip=chip.dataset.dailyChip;
      renderChrome();renderItems();
    });
    document.querySelectorAll('[name="daily-arrange"]').forEach(input=>input.addEventListener('change',()=>{state.grouped=input.value==='type';renderItems()}));
    el('daily-filter-toggle')?.addEventListener('click',()=>{
      const panel=el('daily-filter-panel');panel.hidden=!panel.hidden;
      el('daily-filter-toggle').setAttribute('aria-expanded',String(!panel.hidden));
    });
    document.querySelectorAll('[data-review-filter]').forEach(select=>select.addEventListener('change',()=>{state.filters[select.dataset.reviewFilter]=select.value;renderView();void remediation().catch(()=>{})}));
    el('daily-clear-filters').addEventListener('click',guarded(async()=>{
      state.filters={};state.subjectId='';el('daily-review-subject').value='';
      document.querySelectorAll('[data-review-filter]').forEach(select=>{select.value='';syncCustomSelect(select)});
      window.Cuoti.reviewPage?.navigate?.('daily',{replace:true});
    }));

    el('daily-more-toggle')?.addEventListener('click',()=>{if(menuOpen())closeMenu();else openMenu()});
    document.addEventListener('click',event=>{if(menuOpen()&&!event.target.closest('.daily-more'))closeMenu()});
    el('daily-review-history').addEventListener('click',()=>{closeMenu();void guarded(history)()});
    el('daily-classification-manage').addEventListener('click',()=>{closeMenu();void guarded(()=>window.Cuoti.classification.manager())()});
    el('daily-open-summary')?.addEventListener('click',()=>openDrawer('summary'));
    el('daily-open-generate')?.addEventListener('click',()=>openDrawer('generate'));
    el('daily-open-remediation')?.addEventListener('click',()=>openDrawer('remediation'));

    const drawer=el('daily-drawer');
    el('daily-drawer-close')?.addEventListener('click',closeDrawer);
    el('daily-drawer-scrim')?.addEventListener('click',closeDrawer);
    drawer?.addEventListener('click',event=>{
      if(event.target.closest('[data-drawer-close]')){closeDrawer();return}
      if(event.target.closest('.daily-summary-retry')){void summary();return}
      const step=event.target.closest('[data-step]');
      if(step){const input=el('daily-focus-count');input.value=String(Math.max(5,Math.min(10,(Number(input.value)||5)+Number(step.dataset.step))));return}
      const reopen=event.target.closest('[data-remediation-reopen]');
      if(reopen){reopenRemediation(reopen.dataset.remediationReopen);return}
      const methodLink=event.target.closest('[data-practice-method]');
      if(methodLink){
        event.preventDefault();
        const revision=state.mountRevision;
        void prefillMethod(methodLink.dataset.practiceMethod,methodLink.dataset.subjectId).then(ok=>{if(ok&&revision===state.mountRevision&&drawerOpen())openDrawer('generate')});
        return;
      }
      const link=event.target.closest('a[href*="primary_type_code="]');
      if(link){
        event.preventDefault();
        const code=new URLSearchParams(link.getAttribute('href').split('?')[1]||'').get('primary_type_code');
        void prefillFocused(code).then(ok=>{if(ok)openDrawer('generate')});
      }
    });
    drawer?.addEventListener('change',event=>{
      const target=event.target;
      if(target.name==='daily-practice-mode'){state.practiceMode=target.value;syncModeFields();savePrefs();}
      else if(target.name==='daily-pdf-purpose'||target.name==='daily-pdf-layout')savePrefs();
      else if(['daily-summary-scope','daily-summary-dimension'].includes(target.name)||target.id==='daily-summary-chapter')void summary();
      else if(target.id==='daily-summary-subject')void summaryChapters();
      else if(target.matches('[data-remediation-id]'))updateRemediationSelection();
      else if(target.id==='daily-focus-count'){target.value=String(Math.max(5,Math.min(10,Number(target.value)||5)));}
    });
    el('daily-review-generate-pdf')?.addEventListener('click',()=>void generate());
    el('daily-remediation-generate')?.addEventListener('click',guarded(async()=>{
      const ids=[...el('daily-remediation-content').querySelectorAll('[data-remediation-id]:checked')].map(node=>Number(node.dataset.remediationId));
      if(!ids.length||ids.length>10){showModal('选择再练题目','请选择 1 至 10 道题。','info');return}
      await generate({practice_mode:'focused',practice_kind:'remediation',source_item_ids:ids});
    }));
    el('daily-focus-subject').addEventListener('change',guarded(focusOptions));
    el('daily-focus-method').addEventListener('change',updateMethodDefinition);
    el('daily-undo-button')?.addEventListener('click',undo);
    document.addEventListener('keydown',onKeydown);
    window.addEventListener('pagehide',()=>{void flushQueued()});
    window.addEventListener('cuoti:classification-changed',guarded(loadTypes));
  }

  async function mount(params=new URLSearchParams()){
    const revision=++state.mountRevision;
    bind();
    const drawerView=await applyRouteFilter(params);
    if(revision!==state.mountRevision)return;
    await load();
    if(drawerView&&revision===state.mountRevision)openDrawer(drawerView);
  }

  function deactivate(){
    state.mountRevision+=1;
    state.requestRevision+=1;
    state.requestController?.abort();
    void flushQueued();
    closeMenu();
    if(drawerOpen())renderDrawer(false);
  }

  feature.controller=Object.freeze({bind,load,mount,deactivate,generate,submit:choose,undo,openDrawer,closeDrawer,history,reexportPdf});
})();
