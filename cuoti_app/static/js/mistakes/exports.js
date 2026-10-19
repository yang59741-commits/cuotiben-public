(function initMistakeExports(){
  'use strict';

  const state=window.Cuoti.mistakes.state;
  const apiAdapter=window.Cuoti.mistakes.api;
  let acceptingSuggestions=false;

  function updateBar(){
    const count=state.selectedPublicIds.size;
    const countLabel=document.getElementById('mistake-selected-count');
    const exportButton=document.getElementById('mistake-export-pdf');
    const clearButton=document.getElementById('mistake-clear-selection');
    const selectLoadedButton=document.getElementById('mistake-select-loaded');
    const bulkButton=document.getElementById('mistake-bulk-manage');
    const acceptButton=document.getElementById('mistake-accept-suggestions');
    if(countLabel)countLabel.textContent=`已选 ${count} 道`;
    if(exportButton){
      exportButton.disabled=state.exportInFlight||count===0;
      exportButton.innerHTML=state.exportInFlight
        ?icon('loader','icon-spin')+'正在生成...'
        :icon('download')+'导出 PDF';
    }
    if(clearButton)clearButton.disabled=state.exportInFlight||count===0;
    if(selectLoadedButton)selectLoadedButton.disabled=state.exportInFlight||state.items.length===0;
    if(bulkButton)bulkButton.disabled=state.exportInFlight||count===0;
    if(acceptButton){
      acceptButton.disabled=acceptingSuggestions||state.exportInFlight||count===0;
      acceptButton.innerHTML=acceptingSuggestions?icon('loader','icon-spin')+'正在采纳…':icon('check')+'采纳所选 AI 建议';
    }
    // The selection bar floats over the list only while it has work to do.
    const bar=document.getElementById('mistake-export-bar');
    if(bar)bar.hidden=state.view==='trash'||(count===0&&!state.exportInFlight);
  }

  async function acceptSelectedSuggestions(){
    const publicIds=[...state.selectedPublicIds];
    if(acceptingSuggestions||!publicIds.length)return;
    acceptingSuggestions=true;updateBar();
    let saved=0,skipped=0;
    const errors=[];
    try{
      for(const publicId of publicIds){
        try{
          const typed=await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`);
          const suggestion=typed.suggestion;
          if(!suggestion?.is_current||!suggestion.payload.primary_type_code){skipped++;continue}
          await api(`/api/v2/classification-suggestions/${suggestion.id}/resolve`,{
            method:'POST',body:JSON.stringify({action:'accept',revision:typed.revision,mistake_revision:typed.mistake_revision})
          });
          saved++;
        }catch(error){errors.push(error.message)}
      }
      const message=`已采纳 ${saved} 道${skipped?`；跳过 ${skipped} 道（无有效建议或需先创建题型）`:''}${errors.length?`；失败 ${errors.length} 道：${[...new Set(errors)].join('；')}`:''}`;
      const status=document.getElementById('mistake-export-status');
      if(status)status.textContent=message;
      showToast(message,errors.length||skipped?'warning':'success');
      if(saved)window.dispatchEvent(new Event('cuoti:classification-changed'));
    }finally{acceptingSuggestions=false;updateBar()}
  }

  function openBulk(){
    const publicIds=[...state.selectedPublicIds];
    if(!publicIds.length)return;
    showModal('批量管理',`已选择 ${publicIds.length} 道错题。提交前会先预览影响与冲突。`,'info',
      '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-p btn-sm" id="bulk-preview">生成预览</button>');
    const body=document.getElementById('modal-body');
    const action=document.createElement('select');
    [['trash','移入回收站'],['move_chapter','修改章节'],['set_reasons','替换错因'],['add_tags','添加标签']]
      .forEach(([value,label])=>{
        const option=document.createElement('option');
        option.value=value;option.textContent=label;action.appendChild(option);
      });
    const value=document.createElement('input');
    value.type='text';value.placeholder='章节 ID，或用逗号分隔错因/标签';value.hidden=true;
    const best=document.createElement('label');
    const checkbox=document.createElement('input');checkbox.type='checkbox';
    best.append(checkbox,document.createTextNode(' 允许 best-effort（冲突项跳过并返回重试清单）'));
    action.addEventListener('change',()=>{
      value.hidden=action.value==='trash';
      value.placeholder=action.value==='move_chapter'?'目标章节 ID':
        (action.value==='set_reasons'?'错因，用逗号分隔':'标签，用逗号分隔');
    });
    body.append(document.createElement('br'),action,value,document.createElement('br'),best);
    document.getElementById('bulk-preview').onclick=async()=>{
      try{
        const payload={};
        if(action.value==='move_chapter')payload.chapter_id=Number(value.value);
        if(action.value==='set_reasons')payload.reasons=value.value.split(',').map(item=>item.trim()).filter(Boolean);
        if(action.value==='add_tags')payload.tags=value.value.split(',').map(item=>item.trim()).filter(Boolean);
        const operation={action:action.value,public_ids:publicIds,payload};
        const preview=await apiAdapter.bulkPreview(operation);
        const plan=preview.plan;
        showModal('确认批量操作',`预计修改 ${safeInteger(plan.affected)} 道，冲突 ${safeInteger(plan.conflicts?.length)} 道。`,'warning',
          '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button><button class="btn btn-p btn-sm" id="bulk-commit">确认提交</button>');
        document.getElementById('bulk-commit').onclick=async()=>{
          try{
            const result=await apiAdapter.bulkCommit({
              operation_id:crypto.randomUUID(),preview_token:preview.preview_token,
              mode:checkbox.checked?'best_effort':'atomic',operation
            });
            state.selectedPublicIds.clear();hideModal();
            await window.Cuoti.mistakes.controller.load();
            showToast(`批量操作完成：成功 ${safeInteger(result.succeeded?.length)} 道，失败 ${safeInteger(result.failed?.length)} 道`,result.failed?.length?'warning':'success');
          }catch(error){showModal('批量提交失败',error.message,'error')}
        };
      }catch(error){showModal('批量预览失败',error.message,'error')}
    };
  }

  function setSelected(id,selected,input){
    const publicId=String(state.items.find(item=>safeInteger(item.id)===safeInteger(id))?.public_id||'');
    if(!publicId)return;
    if(selected)state.selectedPublicIds.add(publicId);else state.selectedPublicIds.delete(publicId);
    input?.closest('.mistake-card')?.classList.toggle('selected',selected);
    const status=document.getElementById('mistake-export-status');
    if(status)status.textContent='';
    updateBar();
  }

  function selectAllLoaded(){
    state.items.forEach(item=>{
      if(item.public_id)state.selectedPublicIds.add(String(item.public_id));
    });
    void window.Cuoti.mistakes.controller.render(state.items);
    updateBar();
  }

  function clearSelection(){
    state.selectedPublicIds.clear();
    void window.Cuoti.mistakes.controller.render(state.items);
    updateBar();
  }

  function triggerDownload(result){
    const href=safeLocalURL(result?.download_url);
    if(!href)throw new Error('下载地址无效');
    const link=document.createElement('a');
    link.href=href;link.download=String(result?.download_name||'错题复习.pdf');
    link.hidden=true;document.body.appendChild(link);link.click();link.remove();
  }

  function chooseLayout(){
    if(state.exportInFlight||!state.selectedPublicIds.size)return;
    showModal('选择 PDF 布局','单册最多 100 题；超过时自动生成多个分册。','info',
      '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
      '<button class="btn btn-out btn-sm" id="export-layout-compact">紧凑</button>'+
      '<button class="btn btn-out btn-sm" id="export-layout-notes">笔记留白</button>'+
      '<button class="btn btn-s btn-sm" id="export-layout-two-column">双栏</button>');
    ['compact','notes','two-column'].forEach(name=>{
      document.getElementById('export-layout-'+name).onclick=()=>{
        hideModal();void exportSelected(name.replace('-','_'));
      };
    });
  }

  async function exportSelected(layout='compact'){
    if(state.exportInFlight||!state.selectedPublicIds.size)return;
    const selected=state.items.filter(item=>state.selectedPublicIds.has(String(item.public_id||'')))
      .map(item=>safeInteger(item.id)).filter(Boolean);
    const status=document.getElementById('mistake-export-status');
    state.exportInFlight=true;
    if(status)status.textContent='正在生成 PDF';
    updateBar();
    try{
      const result=await runJobRequest('mistake-export',{mistake_ids:selected,layout});
      triggerDownload(result);state.selectedPublicIds.clear();
      void window.Cuoti.mistakes.controller.render(state.items);
      if(status)status.textContent=`已生成 ${safeInteger(result?.item_count)} 道错题、${safeInteger(result?.volume_count,1)} 个分册`;
    }catch(error){
      if(status)status.textContent='生成失败';
      showModal('导出失败',error.message,'error');
    }finally{
      state.exportInFlight=false;updateBar();
    }
  }

  function formatTime(value,withTime=true){
    const date=new Date(value);
    if(Number.isNaN(date.getTime()))return String(value||'');
    return withTime?date.toLocaleString('zh-CN',{hour12:false}):date.toLocaleDateString('zh-CN');
  }

  function createHistoryRow(item){
    const row=document.createElement('div');row.className='mistake-export-history-item';
    const main=document.createElement('div');main.className='mistake-export-history-main';
    const title=document.createElement('strong');
    const daily=item.export_context==='daily_review';
    const contextLabel=String(item.context_label||(daily?'每日自主复习':'错题复习'));
    const roundLabel=daily&&safeInteger(item.round_number)?` · 第 ${safeInteger(item.round_number)} 轮`:'';
    title.textContent=`${contextLabel}${roundLabel} · ${formatTime(item.created_at)} · ${safeInteger(item.item_count)} 道`;
    const summaries=Array.isArray(item.items)?item.items:[];
    const summary=document.createElement('span');
    const visible=summaries.slice(0,3).map(entry=>{
      const subject=String(entry?.subject||'其他');
      const qnum=String(entry?.qnum||entry?.chapter||'未标号');
      return `${subject} ${qnum}`;
    });
    summary.textContent=visible.join('、')+(summaries.length>3?` 等 ${summaries.length} 道`: '');
    const expiry=document.createElement('small');
    const completion=!daily?'':item.completion_status==='completed'
      ?' · 已完成'
      :item.completion_status==='in_progress'
        ?` · 已完成 ${safeInteger(item.completed_count)} / ${safeInteger(item.item_count)} 道`
        :' · 历史导出（无逐题完成记录）';
    expiry.textContent=`${safeInteger(item.volume_count,1)} 册 · ${String(item.layout||'compact')}${completion} · 保留至 ${formatTime(item.expires_at,false)}`;
    const details=document.createElement('details');
    const detailsTitle=document.createElement('summary');detailsTitle.textContent='完整清单与预览';
    const manifest=document.createElement('ol');
    summaries.forEach(entry=>{
      const line=document.createElement('li');
      line.textContent=`第 ${safeInteger(entry.volume_index,1)} 册 · ${String(entry.subject||'其他')} · ${String(entry.chapter||'未分章节')} · ${String(entry.qnum||'未标号')}`;
      manifest.append(line);
    });
    details.append(detailsTitle,manifest);
    details.addEventListener('toggle',()=>{
      if(!details.open||details.dataset.loaded)return;
      details.dataset.loaded='1';
      (item.volumes||[]).forEach(volume=>{
        const preview=document.createElement('iframe');
        const url=safeLocalURL(String(volume.download_url||'')+'?preview=1');
        if(url){
          preview.src=url;preview.title=`第 ${safeInteger(volume.volume_index,1)} 册 PDF 预览`;
          preview.loading='lazy';details.append(preview);
        }
      });
    });
    main.append(title,summary,expiry,details);
    const download=document.createElement('a');download.className='btn btn-out btn-xs';
    download.href=safeLocalURL(item.download_url);
    download.download=String(item.download_name||'错题复习.pdf');
    download.title='下载此 PDF';download.setAttribute('aria-label',`下载 ${title.textContent}`);
    download.innerHTML=icon('download')+'下载';row.append(main,download);
    return row;
  }

  function renderHistory(){
    const list=document.getElementById('mistake-export-history-list');
    const status=document.getElementById('mistake-export-history-status');
    const more=document.getElementById('mistake-export-history-more');
    if(!list||!status||!more)return;
    list.replaceChildren();
    if(!state.exportHistoryItems.length){
      list.innerHTML=emptyStateHTML('暂无导出记录',{icon:'history',description:'近 30 天生成的间隔复习、每日自主复习和错题 PDF 会显示在这里。',compact:true});
    }else{
      state.exportHistoryItems.forEach(item=>list.appendChild(createHistoryRow(item)));
    }
    status.textContent=state.exportHistoryItems.length
      ?`已显示 ${state.exportHistoryItems.length} 条记录`:'近 30 天暂无记录';
    more.hidden=!state.exportPager.hasMore;
  }

  async function loadHistory(options={}){
    const append=Boolean(options.append);
    if(append&&(!state.exportPager.hasMore||!state.exportPager.nextCursor))return;
    const revision=++state.exportHistoryRevision;
    if(state.exportHistoryController)state.exportHistoryController.abort();
    state.exportHistoryController=new AbortController();
    const status=document.getElementById('mistake-export-history-status');
    const more=document.getElementById('mistake-export-history-more');
    if(!append){state.exportHistoryItems=[];state.exportPager.reset()}
    if(status)status.textContent=append?'正在加载更多...':'正在读取导出历史...';
    if(more)more.disabled=true;
    try{
      const params=state.exportPager.params(append);
      const page=await apiAdapter.exportHistory(params,state.exportHistoryController.signal);
      if(revision!==state.exportHistoryRevision)return;
      const items=Array.isArray(page?.items)?page.items:[];
      const merged=append?[...state.exportHistoryItems,...items]:items;
      const seenIds=new Set();
      state.exportHistoryItems=merged.filter(item=>{
        const id=safeInteger(item?.id);
        if(!id||seenIds.has(id))return false;
        seenIds.add(id);return true;
      });
      state.exportPager.nextCursor=page?.next_cursor?String(page.next_cursor):null;
      state.exportPager.hasMore=Boolean(page?.has_more&&state.exportPager.nextCursor);
      renderHistory();
    }catch(error){
      if(error?.name==='AbortError'||revision!==state.exportHistoryRevision)return;
      if(status)status.textContent='导出历史读取失败：'+error.message;
    }finally{
      if(revision===state.exportHistoryRevision&&more)more.disabled=false;
    }
  }

  function openHistory(){
    showModal('错题导出历史','','info',
      '<button type="button" class="btn btn-p btn-sm" onclick="hideModal()">关闭</button>');
    document.querySelector('#modal .modal-box')?.classList.add('modal-box-wide');
    const body=document.getElementById('modal-body');
    body.className='modal-body mistake-export-history-modal';
    const status=document.createElement('div');
    status.className='mistake-export-history-status';status.id='mistake-export-history-status';
    status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const list=document.createElement('div');
    list.className='mistake-export-history-list';list.id='mistake-export-history-list';
    const more=document.createElement('button');
    more.type='button';more.className='btn btn-out btn-sm mistake-export-history-more';
    more.id='mistake-export-history-more';more.textContent='加载更多';more.hidden=true;
    more.addEventListener('click',()=>loadHistory({append:true}));
    body.replaceChildren(status,list,more);void loadHistory();
  }

  window.Cuoti.mistakes.exports=Object.freeze({
    updateBar,openBulk,acceptSelectedSuggestions,setSelected,selectAllLoaded,clearSelection,
    triggerDownload,chooseLayout,exportSelected,formatTime,createHistoryRow,
    renderHistory,loadHistory,openHistory
  });
})();
