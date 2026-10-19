(function initExamIssues(){
  'use strict';
  const exam=window.Cuoti.exam;
  const el=id=>document.getElementById(id);
  const categories={formula:'公式显示',condition:'条件缺失',merged:'题目粘连',classification:'分类不准',other:'其他'};
  const statuses={open:'待处理',resolved:'已处理',dismissed:'无需处理'};
  const reports=new Map(),edits=new Map();
  let preview=null,view=null,binding=null,previousFocus=null;
  let listRevision=0,listController=null,rows=[],cursor=null,total=0;

  function reportDraft(){
    if(!reports.has(preview.id))reports.set(preview.id,{item:'',category:'',description:'',saving:false,error:'',request:null});
    return reports.get(preview.id);
  }
  function editDraft(row){
    const existing=edits.get(row.public_id);
    if(!existing||(!existing.dirty&&!existing.saving&&existing.revision!==row.revision))edits.set(row.public_id,{revision:row.revision,status:row.status,note:row.handling_note,saving:false,error:'',current:null,dirty:false});
    return edits.get(row.public_id);
  }
  function visible(){return Boolean(view&&!el('exam-issues-drawer')?.hidden);}
  function showStatus(message,kind=''){
    const node=el('exam-issues-message');node.textContent=message;node.className='exam-issues-message'+(kind?' is-'+kind:'');
  }
  function setPreview(data){
    close();preview=data;
    const toolbar=el('exam-issues-toolbar');
    if(toolbar)toolbar.hidden=!data;
    if(data&&el('exam-issue-report-open'))el('exam-issue-report-open').disabled=!data.items?.length;
  }
  function renderReport(){
    const draft=reportDraft();
    const select=el('exam-issue-question');
    select.replaceChildren(new Option('请选择题目',''),...preview.items.map(item=>new Option(`第 ${item.position} 题 · ${item.question_type||'题目'}`,item.public_id)));
    select.value=draft.item;syncCustomSelect(select);
    el('exam-issue-category').value=draft.category;syncCustomSelect(el('exam-issue-category'));
    el('exam-issue-description').value=draft.description;
    el('exam-issue-form').querySelectorAll('input,select,textarea,button').forEach(node=>{node.disabled=draft.saving});
    const save=el('exam-issue-submit');save.disabled=draft.saving||!draft.item||!draft.category||!draft.description.trim();
    save.textContent=draft.saving?'正在保存…':'保存问题';
    showStatus(draft.saving?'正在保存…':draft.error,draft.saving?'busy':draft.error?'error':'');
    renderExcerpt();
  }
  function renderExcerpt(){
    const draft=reportDraft(),item=preview.items.find(row=>row.public_id===draft.item);
    el('exam-issue-excerpt').textContent=item?.content||'';
    const count=el('exam-issue-count'),length=draft.description.length;
    count.textContent=`${length} / 2000${length===2000?' · 已达上限':length>=1800?' · 接近上限':''}`;
    count.className='exam-issue-count'+(length===2000?' is-full':length>=1800?' is-near':'');
  }
  function readReport(){
    const draft=reportDraft();
    Object.assign(draft,{item:el('exam-issue-question').value,category:el('exam-issue-category').value,description:el('exam-issue-description').value,error:''});
    el('exam-issue-submit').disabled=draft.saving||!draft.item||!draft.category||!draft.description.trim();
    showStatus('');renderExcerpt();
  }
  function open(next){
    if(next==='report'&&!preview)return;
    if(!visible())previousFocus=document.activeElement;
    view=next;
    el('exam-issues-drawer').hidden=false;el('exam-issues-scrim').hidden=false;
    document.querySelectorAll('.main,.sb').forEach(node=>node.setAttribute('inert',''));
    el('exam-issues-drawer').querySelectorAll('[data-issue-view]').forEach(node=>{node.hidden=node.dataset.issueView!==view});
    el('exam-issues-title').textContent=next==='report'?'记录题目问题':'问题记录';
    el('exam-issues-subtitle').textContent=preview?.title||'全部试卷';
    el('exam-issues-subtitle').title=preview?.title||'全部试卷';
    el('exam-issues-drawer').querySelectorAll('[data-issue-switch="report"]').forEach(node=>{node.hidden=!preview?.items?.length});
    const scope=el('exam-issues-scope');
    const currentOption=[...scope.options].find(option=>option.value==='current');
    if(currentOption)currentOption.disabled=!preview;
    if(!preview)scope.value='all';
    syncCustomSelect(scope);
    showStatus('');
    if(view==='report'){listRevision+=1;listController?.abort();renderReport();}
    else void load();
    el('exam-issues-close').focus();
  }
  function close(){
    listRevision+=1;listController?.abort();
    if(!visible()){view=null;return;}
    view=null;el('exam-issues-drawer').hidden=true;el('exam-issues-scrim').hidden=true;
    document.querySelectorAll('.main,.sb').forEach(node=>node.removeAttribute('inert'));
    if(previousFocus?.isConnected&&previousFocus.getClientRects().length)previousFocus.focus({preventScroll:true});
    previousFocus=null;
  }
  async function submit(event){
    event.preventDefault();
    const draft=reportDraft();
    if(draft.saving||!el('exam-issue-form').reportValidity())return;
    readReport();
    const eid=preview.id;
    const body={exam_item_public_id:draft.item,category:draft.category,description:draft.description.trim()};
    const signature=JSON.stringify(body);
    if(draft.request?.signature!==signature)draft.request={signature,id:crypto.randomUUID()};
    draft.saving=true;renderReport();
    try{
      await exam.api.request(`/api/v2/exams/${eid}/question-issues`,{method:'POST',body:JSON.stringify({...body,client_event_id:draft.request.id})});
      reports.delete(eid);
      if(visible()&&view==='report'&&preview.id===eid){open('list');showStatus('已记录，可在问题记录中跟进处理。','success');}
    }catch(error){
      if(error.name!=='AbortError')draft.error='保存未完成，内容已保留。'+error.message;
    }finally{
      draft.saving=false;
      if(visible()&&view==='report'&&preview.id===eid)renderReport();
    }
  }
  function renderRows(){
    const root=el('exam-issue-list');
    const statusIcon={open:'clock',resolved:'circleCheck',dismissed:'archive'};
    const time=value=>new Date(value).toLocaleString('zh-CN',{hour12:false});
    root.innerHTML=rows.length?rows.map(row=>{
      const draft=editDraft(row),snapshot=row.snapshot||{};
      const id=escapeHTML(row.public_id),disabled=draft.saving?' disabled':'';
      const noteId=`issue-note-${id}`;
      const editing=row.status==='open'||draft.dirty||draft.current;
      return `<article class="exam-issue-card${draft.saving?' is-saving':''}" data-status="${row.status}" data-issue-id="${id}" aria-busy="${draft.saving}">
        <div class="exam-issue-card-heading"><h3>第 ${safeInteger(snapshot.position)} 题<span class="exam-issue-label">${escapeHTML(categories[row.category]||row.category)}</span></h3><span class="exam-issue-status" data-status="${row.status}">${icon(statusIcon[row.status])}${escapeHTML(statuses[row.status])}</span></div>
        <p class="exam-issue-meta">记录于 ${escapeHTML(time(row.created_at))}</p>
        <p class="exam-issue-source"><strong>${escapeHTML(snapshot.exam_title||'历史试卷')}</strong>${row.exam_deleted?`<span class="exam-issue-label is-trashed">${icon('trash')}试卷已移入回收站</span>`:''}</p>
        <div><span class="exam-issue-caption">原始描述</span><p class="exam-issue-description">${escapeHTML(row.description)}</p></div>
        <details class="exam-issue-question-snapshot"><summary>查看题目与来源</summary><p>${escapeHTML(snapshot.content||'')}</p><small>${escapeHTML([snapshot.subject,snapshot.source_kind,snapshot.source_candidate_id||snapshot.source_mistake_public_id].filter(Boolean).join(' · '))}</small></details>
        ${draft.current?`<div class="exam-issue-conflict"><strong class="exam-issue-conflict-title">${icon('circleAlert')}记录已在其他页面更新</strong><dl><dt>最新状态</dt><dd>${escapeHTML(statuses[draft.current.status])}</dd><dt>最新备注</dt><dd>${escapeHTML(draft.current.handling_note||'无')}</dd></dl><p>你的草稿已保留。读取最新版本后，请核对再保存。</p><button type="button" class="btn btn-out btn-sm" data-issue-latest>读取最新版本，保留草稿</button></div>`:''}
        <div class="exam-issue-handle">${editing?`<label for="${noteId}"><span class="exam-issue-field-label">处理备注</span><textarea id="${noteId}" data-issue-note maxlength="2000" rows="2"${disabled}>${escapeHTML(draft.note)}</textarea></label><fieldset><legend class="exam-issue-field-label">处理结果</legend><div class="review-generation-mode">${Object.entries(statuses).map(([value,label])=>`<label><input type="radio" name="issue-state-${id}" data-issue-status value="${value}"${draft.status===value?' checked':''}${disabled}><span>${label}</span></label>`).join('')}</div></fieldset>`:`<span class="exam-issue-field-label">处理备注</span><p class="exam-issue-note">${escapeHTML(row.handling_note)}</p>`}</div>
        ${draft.error&&!draft.current?`<div class="exam-issue-error">${escapeHTML(draft.error)}</div>`:''}
        <div class="exam-issue-actions">${draft.dirty?'<span class="exam-issue-dirty">有未保存的修改</span>':''}${editing?`<button type="button" class="btn btn-p btn-sm" data-issue-save${draft.saving||draft.current?' disabled':''}>${draft.saving?'保存中…':'保存更改'}</button>`:''}${row.status!=='open'?`<button type="button" class="btn btn-out btn-sm" data-issue-reopen${draft.saving||draft.current?' disabled':''}>重新打开</button>`:''}</div>
      </article>`;
    }).join(''):emptyStateHTML('暂无问题记录',{icon:'listChecks',compact:true,description:'可调整筛选范围，或从试卷预览中记录题面问题。'});
    el('exam-issues-total').textContent=`已显示 ${rows.length} / ${total} 条`;
    el('exam-issues-more').hidden=!cursor;
  }
  async function load(more=false){
    const revision=++listRevision;
    listController?.abort();listController=new AbortController();
    showStatus('');
    const params=new URLSearchParams({limit:'20'});
    const currentScope=el('exam-issues-scope').value==='current';
    el('exam-issues-subtitle').textContent=currentScope?preview.title:'全部试卷 · 按记录时间从新到旧';
    el('exam-issues-subtitle').title=el('exam-issues-subtitle').textContent;
    if(currentScope)params.set('exam_id',preview.id);
    if(el('exam-issues-status').value)params.set('status',el('exam-issues-status').value);
    if(more&&cursor)params.set('after',cursor);
    if(!more){rows=[];cursor=null;el('exam-issue-list').innerHTML='<p class="exam-issues-loading">正在读取问题记录…</p>';el('exam-issues-total').textContent='';}
    el('exam-issues-more').hidden=true;
    el('exam-issues-retry').hidden=true;
    try{
      const result=await exam.api.request('/api/v2/question-issues?'+params,{signal:listController.signal});
      if(revision!==listRevision||!visible()||view!=='list')return;
      rows=more?[...rows,...result.items]:result.items;cursor=result.next_cursor;total=result.total;
      renderRows();
    }catch(error){
      if(error.name==='AbortError'||revision!==listRevision||!visible())return;
      showStatus('读取失败：'+error.message,'error');
      el('exam-issues-retry').hidden=false;
      if(more)el('exam-issues-more').hidden=!cursor;
      else el('exam-issue-list').textContent='暂时无法读取问题记录，请重试。';
    }
  }
  async function save(id,reopen=false){
    const row=rows.find(item=>item.public_id===id),draft=editDraft(row);
    if(draft.saving||draft.current)return;
    if(reopen){draft.status='open';draft.dirty=true;}
    draft.saving=true;draft.error='';
    renderRows();
    try{
      const saved=await exam.api.request('/api/v2/question-issues/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify({revision:draft.revision,status:draft.status,handling_note:draft.note})});
      const current=rows.find(item=>item.public_id===id);
      if(current)Object.assign(current,saved);
      const filter=el('exam-issues-status').value;
      if(current&&filter&&saved.status!==filter){rows=rows.filter(item=>item.public_id!==id);total-=1;}
      edits.delete(id);
      if(current&&visible()&&view==='list')showStatus('处理记录已保存。','success');
    }catch(error){
      if(error.name!=='AbortError'){
        draft.error='保存未完成，草稿已保留。'+error.message;
        if(error.data?.code==='REVISION_CONFLICT')draft.current=error.data.current;
        if(visible()&&view==='list')showStatus(`第 ${row.snapshot.position} 题保存未完成，草稿已保留。`,'error');
      }
    }finally{
      draft.saving=false;
      if(visible()&&view==='list'){
        renderRows();
        el('exam-issue-list').querySelector(`[data-issue-id="${id}"] button:not([disabled])`)?.focus({preventScroll:true});
      }
    }
  }
  function listInput(event){
    const card=event.target.closest('[data-issue-id]');if(!card)return;
    const draft=editDraft(rows.find(row=>row.public_id===card.dataset.issueId));
    if(event.target.matches('[data-issue-note]'))draft.note=event.target.value;
    if(event.target.matches('[data-issue-status]'))draft.status=event.target.value;
    draft.dirty=true;
  }
  function keydown(event){
    if(!visible()||event.defaultPrevented)return;
    if(event.key==='Escape'){event.preventDefault();close();return;}
    if(event.key!=='Tab')return;
    const nodes=[...el('exam-issues-drawer').querySelectorAll('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,a[href],[tabindex="0"]')].filter(node=>!node.closest('[hidden]')&&node.getClientRects().length);
    const first=nodes[0],last=nodes.at(-1);
    if(!el('exam-issues-drawer').contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first)?.focus();}
    else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
  function bind(){
    if(binding)return;binding=new AbortController();
    for(const id of ['exam-issues-drawer','exam-issues-scrim']){
      const node=el(id);if(node&&node.parentElement!==document.body)document.body.appendChild(node);
    }
    const on=(id,name,handler)=>el(id)?.addEventListener(name,handler,{signal:binding.signal});
    on('exam-issue-report-open','click',()=>open('report'));on('exam-issue-list-open','click',()=>open('list'));
    on('exam-issues-history-open','click',()=>open('list'));
    on('exam-issues-close','click',close);on('exam-issues-scrim','click',close);on('exam-issues-done','click',close);
    on('exam-issues-drawer','click',event=>{
      if(event.target.closest('[data-issues-close]'))close();
      const target=event.target.closest('[data-issue-switch]');if(target)open(target.dataset.issueSwitch);
    });
    on('exam-issue-form','input',readReport);on('exam-issue-form','change',readReport);on('exam-issue-form','submit',submit);
    on('exam-issues-scope','change',()=>void load());on('exam-issues-status','change',()=>void load());
    on('exam-issues-more','click',()=>void load(true));on('exam-issues-retry','click',()=>{showStatus('');void load()});
    on('exam-issue-list','input',listInput);on('exam-issue-list','change',listInput);
    on('exam-issue-list','click',event=>{
      const card=event.target.closest('[data-issue-id]');if(!card)return;
      const id=card.dataset.issueId;
      if(event.target.closest('[data-issue-save]'))void save(id);
      else if(event.target.closest('[data-issue-reopen]'))void save(id,true);
      else if(event.target.closest('[data-issue-latest]')){
        const draft=edits.get(id);
        Object.assign(rows.find(row=>row.public_id===id),draft.current);
        draft.revision=draft.current.revision;draft.current=null;draft.error='已读取最新版本，你的草稿保持原样。请核对后保存。';renderRows();
        el('exam-issue-list').querySelector(`[data-issue-id="${id}"] [data-issue-save]`)?.focus({preventScroll:true});
      }
    });
    document.addEventListener('keydown',keydown,{signal:binding.signal});
  }
  function unbind(){close();binding?.abort();binding=null;preview=null;}
  exam.issues=Object.freeze({bind,unbind,setPreview,open,close});
})();
