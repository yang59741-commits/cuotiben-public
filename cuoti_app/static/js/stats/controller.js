(function initStatsController(){
  'use strict';

  const F=window.Cuoti.stats.format;
  const C=window.Cuoti.stats.charts;
  const R=window.Cuoti.stats.render;
  const byId=id=>document.getElementById(id);
  const PRESETS=Object.freeze(['7d','30d','90d','term']);
  const ROUTE_KEYS=Object.freeze(['range','from','to','subject_id','chapter_id']);
  const MAX_RANGE_DAYS=730;
  const state={preset:'30d',from:'',to:'',subjectId:'',chapterId:'',expanded:false,table:false,data:null,reminder:null,trendRows:[]};
  let initialized=false,subjectsReady=null,requestRevision=0,requestController=null;
  let chapterRevision=0,chapterController=null,menu=null,resizeTimer=0,lastWidth=0;

  function setStatsInlineStatus(message,type=''){
    const status=byId('stats-inline-status');
    if(!status)return;
    status.textContent=message||'';status.className='stats-inline-status'+(type?' '+type:'');status.hidden=!message;
  }
  function todayKey(){return F.dayKey(new Date())}
  function applyPreset(preset){
    const range=F.statsPresetRange(preset);
    if(!range)return false;
    state.preset=preset;state.from=F.dayKey(range[0]);state.to=F.dayKey(range[1]);
    return true;
  }
  function validRange(from,to){
    return Boolean(F.parseDay(from)&&F.parseDay(to))&&from<=to&&to<=todayKey()&&F.diffDays(from,to)<MAX_RANGE_DAYS;
  }

  // ── Scope bar ──
  function selectedText(select){return select?.selectedOptions?.[0]?.textContent||''}
  function renderScope(){
    document.querySelectorAll('[data-stats-preset]').forEach(button=>{
      const on=button.dataset.statsPreset===state.preset;
      button.classList.toggle('on',on);button.setAttribute('aria-pressed',String(on));
    });
    const sameYear=state.from.slice(0,4)===state.to.slice(0,4)&&state.to.slice(0,4)===todayKey().slice(0,4);
    const label=sameYear?day=>F.dayLabel(day):day=>F.formatStatsDate(day);
    byId('stats-range').textContent=state.from&&state.to?`${label(state.from)} – ${label(state.to)}`:'';
    const chapterName=state.chapterId?selectedText(byId('stats-chapter')):'';
    byId('stats-drill').innerHTML=state.chapterId&&chapterName
      ?`<span class="stats-chip">只看：${escapeHTML(chapterName)}<button type="button" data-stats-clear-chapter aria-label="取消章节筛选">${icon('x')}</button></span>`:'';
  }
  function loadSubjects(){
    if(subjectsReady)return subjectsReady;
    const subject=byId('stats-subject');
    subjectsReady=(async()=>{
      try{
        const subjects=await api('/api/subjects');
        subjects.forEach(item=>subject.add(new Option(String(item.name||''),String(safeInteger(item.id,'')))));
        subject.value=state.subjectId;
      }catch(error){subject.disabled=true;setStatsInlineStatus('科目筛选暂不可用，仍可查看全部统计。','error')}
      syncCustomSelect(subject);
    })();
    return subjectsReady;
  }
  async function loadChapterOptions(subjectId,selected=''){
    const chapter=byId('stats-chapter');
    const revision=++chapterRevision;
    chapterController?.abort();
    chapter.replaceChildren(new Option(subjectId?'正在读取章节...':'请先选择科目',''));
    chapter.disabled=true;
    chapter.toggleAttribute('aria-busy',Boolean(subjectId));
    syncCustomSelect(chapter);
    if(!subjectId)return;
    chapterController=new AbortController();
    try{
      const chapters=await api('/api/chapters?'+new URLSearchParams({subject_id:subjectId}).toString(),{signal:chapterController.signal});
      if(revision!==chapterRevision)return;
      if(!Array.isArray(chapters)||!chapters.length){
        chapter.replaceChildren(new Option('该科目暂无可选章节',''));
      }else{
        chapter.replaceChildren(new Option('全部章节',''));
        chapters.forEach(item=>chapter.add(new Option(String(item.name||''),String(safeInteger(item.id,'')))));
        chapter.disabled=false;
        chapter.value=selected;
      }
      syncCustomSelect(chapter);
    }catch(error){
      if(error?.name==='AbortError'||revision!==chapterRevision)return;
      chapter.replaceChildren(new Option('章节筛选暂不可用',''));
      syncCustomSelect(chapter);
      setStatsInlineStatus('章节筛选暂不可用，当前统计内容仍保留。','error');
    }finally{
      if(revision===chapterRevision)chapter.removeAttribute('aria-busy');
    }
  }
  async function setSubjectAndChapter(subjectId,chapterId){
    await loadSubjects();
    const subject=byId('stats-subject');
    state.subjectId=subjectId;
    subject.value=subjectId;
    if(subject.value!==subjectId){state.subjectId='';subject.value=''}
    syncCustomSelect(subject);
    await loadChapterOptions(state.subjectId,chapterId);
    state.chapterId=state.subjectId&&byId('stats-chapter').value===chapterId?chapterId:'';
  }

  // ── Route: filters live in the hash so drill-downs can be undone with Back ──
  function scopeParams(){
    const params=new URLSearchParams();
    if(state.preset!=='custom')params.set('range',state.preset);
    else{params.set('from',state.from);params.set('to',state.to)}
    if(state.subjectId)params.set('subject_id',state.subjectId);
    if(state.chapterId)params.set('chapter_id',state.chapterId);
    return params;
  }
  function syncRoute(push=false){
    const target=routeURL('stats',scopeParams());
    const current=window.location.pathname+window.location.search+window.location.hash;
    if(target===current)return;
    window.history[push?'pushState':'replaceState']({route:'stats'},'',target);
  }
  async function applyRouteParams(params){
    if(!(params instanceof URLSearchParams)||!ROUTE_KEYS.some(key=>params.has(key)))return false;
    const preset=params.get('range'),from=params.get('from')||'',to=params.get('to')||'';
    if(PRESETS.includes(preset))applyPreset(preset);
    else if(validRange(from,to)){state.preset='custom';state.from=from;state.to=to}
    else applyPreset('30d');
    const subjectId=String(safeInteger(params.get('subject_id'),0)||'');
    const chapterId=subjectId?String(safeInteger(params.get('chapter_id'),0)||''):'';
    await setSubjectAndChapter(subjectId,chapterId);
    return true;
  }

  // ── Loading: skeleton on first paint, later refreshes keep the previous frame ──
  async function loadStats({entering=false}={}){
    if(state.preset!=='custom')applyPreset(state.preset);
    renderScope();
    const revision=++requestRevision;
    requestController?.abort();
    requestController=new AbortController();
    const dash=byId('stats-dash'),busy=byId('stats-busy');
    if(!state.data){dash.hidden=false;R.skeleton()}else dash.classList.add('is-refreshing');
    busy.hidden=false;
    const params=new URLSearchParams({date_from:state.from,date_to:state.to});
    if(state.subjectId)params.set('subject_id',state.subjectId);
    if(state.chapterId)params.set('chapter_id',state.chapterId);
    const reminderParams=new URLSearchParams();
    if(state.subjectId)reminderParams.set('subject_id',state.subjectId);
    if(state.chapterId)reminderParams.set('chapter_id',state.chapterId);
    let d,reminder;
    try{
      [d,reminder]=await Promise.all([
        api('/api/v2/stats?'+params,{signal:requestController.signal}),
        api('/api/v2/stats/reminders'+(reminderParams.size?'?'+reminderParams:''),{signal:requestController.signal})
      ]);
    }catch(error){
      if(revision!==requestRevision||error?.name==='AbortError')return;
      busy.hidden=true;dash.classList.remove('is-refreshing');
      setStatsInlineStatus('统计读取失败，请稍后重试。','error');
      const errorBox=byId('stats-load-error');
      errorBox.hidden=false;
      errorBox.querySelector('span').textContent=state.data?'当前统计暂时无法更新，已保留上次显示内容。':'统计暂时无法读取。';
      if(!state.data)dash.hidden=true;
      return;
    }
    if(revision!==requestRevision)return;
    busy.hidden=true;dash.classList.remove('is-refreshing');dash.hidden=false;
    setStatsInlineStatus('');
    byId('stats-load-error').hidden=true;
    void window.Cuoti.reviewBadge?.refresh?.();
    state.data=d;state.reminder=reminder;state.trendRows=F.denseTrend(d.review_trend,d.scope);
    renderAll({entering});
    window.Cuoti.daily.render();
    renderMath(document.getElementById('p-stats'));
  }
  function renderAll({entering=false}={}){
    const d=state.data,scope={subjectId:state.subjectId,chapterId:state.chapterId};
    closeMenu();C.hideTip();
    R.completeness(d);
    R.hero(d,state.reminder,{entering,...scope});
    R.ledger(d,state.trendRows);
    R.rhythm(d,state.trendRows,{table:state.table});
    R.chapters(d,{expanded:state.expanded});
    R.reasons(d);
    R.insights(d,scope);
    R.exam(d,scope);
    R.trail(d,scope);
    if(entering)setTimeout(()=>byId('stats-hero')?.classList.remove('is-entering'),1400);
  }
  function rerenderLayout(){
    if(!state.data)return;
    R.layoutPipe();
    R.rhythm(state.data,state.trendRows,{table:state.table});
  }

  // ── Chapter menu (drill-down) ──
  function closeMenu(){
    if(!menu)return;
    menu.anchor.setAttribute('aria-expanded','false');
    menu.element.remove();
    menu=null;
  }
  function openMenu(row){
    closeMenu();
    const element=document.createElement('div');
    element.className='stats-menu';
    element.setAttribute('role','menu');
    const name=row.querySelector('.chapter-name>span')?.textContent||'';
    const scope=` data-subject-id="${safeInteger(row.dataset.subjectId)}" data-chapter-id="${safeInteger(row.dataset.chapterId)}"`;
    element.innerHTML=`<div class="stats-menu-title">${escapeHTML(name)}</div><button type="button" role="menuitem" data-action="open-spaced-review"${scope}>${icon('repeat')}复习本章到期题</button><button type="button" role="menuitem" data-stats-menu-focus>${icon('filter')}只看本章统计</button>`;
    document.body.append(element);
    const box=row.getBoundingClientRect();
    element.style.left=Math.max(8,Math.min(window.innerWidth-element.offsetWidth-8,box.left+Math.min(160,box.width/3)))+'px';
    element.style.top=(box.bottom+element.offsetHeight+8>window.innerHeight?box.top-element.offsetHeight-4:box.bottom+4)+'px';
    row.setAttribute('aria-expanded','true');
    menu={element,anchor:row};
    element.querySelector('button').focus();
    element.addEventListener('click',event=>{
      if(event.target.closest('[data-action]')){closeMenu();return}
      if(!event.target.closest('[data-stats-menu-focus]'))return;
      const subjectId=row.dataset.subjectId,chapterId=row.dataset.chapterId;
      closeMenu();
      void focusChapter(subjectId,chapterId);
    });
    element.addEventListener('keydown',event=>{
      const items=[...element.querySelectorAll('button')],index=items.indexOf(document.activeElement);
      if(event.key==='Escape'){event.preventDefault();row.focus();closeMenu()}
      else if(event.key==='Tab')closeMenu();
      else if(event.key==='ArrowDown'||event.key==='ArrowUp'){
        event.preventDefault();
        items[(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
      }
    });
  }
  async function focusChapter(subjectId,chapterId){
    await setSubjectAndChapter(String(safeInteger(subjectId,0)||''),String(safeInteger(chapterId,0)||''));
    state.expanded=false;
    syncRoute(true);
    byId('stats-scope').scrollIntoView({block:'nearest'});
    await loadStats();
  }

  // ── Popovers ──
  function setPopover(button,pop,open){
    pop.hidden=!open;
    button?.setAttribute('aria-expanded',String(open));
  }
  function closePopovers(){
    setPopover(byId('stats-completeness'),byId('stats-completeness-pop'),false);
    setPopover(document.querySelector('[data-stats-preset="custom"]'),byId('stats-custom-pop'),false);
  }
  function openCustomRange(){
    const from=byId('stats-date-from'),to=byId('stats-date-to'),today=todayKey();
    from.value=state.from;to.value=state.to;from.max=today;to.max=today;
    setPopover(byId('stats-completeness'),byId('stats-completeness-pop'),false);
    setPopover(document.querySelector('[data-stats-preset="custom"]'),byId('stats-custom-pop'),true);
    from.focus();
  }
  function applyCustomRange(){
    const from=byId('stats-date-from').value,to=byId('stats-date-to').value;
    if(!validRange(from,to)){
      showToast('日期范围无效：开始不能晚于结束，结束不能晚于今天，最长 730 天','error');
      return;
    }
    state.preset='custom';state.from=from;state.to=to;
    closePopovers();
    syncRoute(false);
    void loadStats();
  }

  function isActivePage(){return Boolean(byId('p-stats')?.classList.contains('on'))}
  function bindOnce(){
    if(initialized)return;
    initialized=true;
    byId('stats-scope').addEventListener('click',event=>{
      const preset=event.target.closest('[data-stats-preset]');
      if(preset){
        const key=preset.dataset.statsPreset;
        if(key==='custom'){byId('stats-custom-pop').hidden?openCustomRange():closePopovers();return}
        closePopovers();
        if(applyPreset(key)){syncRoute(false);void loadStats()}
        return;
      }
      if(event.target.closest('#stats-completeness')){
        const pop=byId('stats-completeness-pop'),open=pop.hidden;
        closePopovers();setPopover(byId('stats-completeness'),pop,open);
        return;
      }
      if(event.target.closest('[data-stats-clear-chapter]')){
        state.chapterId='';byId('stats-chapter').value='';syncCustomSelect(byId('stats-chapter'));
        syncRoute(false);void loadStats();
      }
    });
    byId('stats-apply').addEventListener('click',applyCustomRange);
    byId('stats-custom-cancel').addEventListener('click',closePopovers);
    byId('stats-custom-pop').addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('input')){event.preventDefault();applyCustomRange()}});
    byId('stats-subject').addEventListener('change',async event=>{
      const subjectId=event.target.value;
      state.subjectId=subjectId;state.chapterId='';
      setStatsInlineStatus('');
      syncRoute(false);
      void loadStats();
      await loadChapterOptions(subjectId);
    });
    byId('stats-chapter').addEventListener('change',event=>{
      state.chapterId=event.target.value;
      syncRoute(false);void loadStats();
    });
    byId('stats-dash').addEventListener('click',event=>{
      const target=event.target;
      const row=target.closest('.chapter-row');
      if(row){menu?.anchor===row?closeMenu():openMenu(row);return}
      if(target.closest('[data-stats-expand]')){state.expanded=!state.expanded;R.chapters(state.data,{expanded:state.expanded});return}
      const chip=target.closest('[data-stats-focus-chapter]');
      if(chip){void focusChapter(chip.dataset.statsFocusSubject,chip.dataset.statsFocusChapter);return}
    });
    byId('stats-rhythm-tools').addEventListener('click',event=>{
      if(!event.target.closest('[data-stats-table]')||!state.data)return;
      state.table=!state.table;
      R.rhythm(state.data,state.trendRows,{table:state.table});
    });
    document.addEventListener('click',event=>{
      if(!isActivePage())return;
      const target=event.target;
      if(menu&&!target.closest('.stats-menu')&&!target.closest('.chapter-row'))closeMenu();
      if(!target.closest('.stats-pop')&&!target.closest('#stats-completeness')&&!target.closest('[data-stats-preset="custom"]'))closePopovers();
    });
    document.addEventListener('keydown',event=>{
      if(event.key!=='Escape'||!isActivePage())return;
      const custom=!byId('stats-custom-pop').hidden;
      closeMenu();closePopovers();
      if(custom)document.querySelector('[data-stats-preset="custom"]')?.focus();
    });
    document.addEventListener('scroll',()=>{closeMenu();C.hideTip()},{capture:true,passive:true});
    if(window.ResizeObserver){
      new ResizeObserver(([entry])=>{
        const width=Math.round(entry.contentRect.width);
        if(!width||Math.abs(width-lastWidth)<6)return;
        lastWidth=width;
        clearTimeout(resizeTimer);
        resizeTimer=setTimeout(rerenderLayout,140);
      }).observe(byId('stats-dash'));
    }
  }

  async function mountStats(context={}){
    bindOnce();
    if(!state.from)applyPreset('30d');
    renderScope();
    const subjects=loadSubjects();
    if(!await applyRouteParams(context.params)){await subjects;syncRoute(false)}
    await window.Cuoti.daily.load();
    await loadStats({entering:true});
  }
  async function refreshStats(context={}){
    bindOnce();
    if(!await applyRouteParams(context?.params))syncRoute(false);
    await loadStats();
  }
  function unmountStats(){
    requestRevision+=1;
    chapterRevision+=1;
    requestController?.abort();
    chapterController?.abort();
    byId('stats-busy').hidden=true;
    byId('stats-dash').classList.remove('is-refreshing');
    closeMenu();closePopovers();C.hideTip();
  }

  window.Cuoti.stats.controller=Object.freeze({loadStats,mount:mountStats,unmount:unmountStats,refresh:refreshStats});
})();
