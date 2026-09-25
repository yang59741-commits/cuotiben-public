(function initMistakeEditors(){
  'use strict';

  const state=window.Cuoti.mistakes.state;
  const apiAdapter=window.Cuoti.mistakes.api;
  const chapterCache=new Map();

  function conflictPayload(error){
    const data=error?.data&&typeof error.data==='object'?error.data:{};
    return {
      ...data,
      latest:data.latest&&typeof data.latest==='object'?data.latest:null,
      currentRevision:safeInteger(data.current_revision),
      actions:Array.isArray(data.actions)?data.actions:[]
    };
  }

  function showRevisionConflict(error,{onLoadLatest,onKeepDraft,onRetry}={}){
    const data=conflictPayload(error);
    const latest=data.latest;
    const summary=latest
      ?`当前版本为 ${data.currentRevision||safeInteger(latest.revision)}，${latest.status?'状态：'+latest.status+'。':''}请先确认最新内容。`
      :'这道错题已不存在，请刷新当前列表。';
    const buttons=[];
    if(latest&&onLoadLatest)buttons.push('<button type="button" class="btn btn-out btn-sm" id="revision-load-latest">载入最新版本</button>');
    if(latest&&onKeepDraft)buttons.push('<button type="button" class="btn btn-out btn-sm" id="revision-keep-draft">保留当前草稿</button>');
    if(latest&&onRetry)buttons.push('<button type="button" class="btn btn-p btn-sm" id="revision-retry">重新应用修改</button>');
    buttons.push('<button type="button" class="btn btn-out btn-sm" id="revision-close">关闭</button>');
    showModal('内容已更新',summary,'warning',buttons.join(''));
    document.getElementById('revision-load-latest')?.addEventListener('click',()=>{hideModal();onLoadLatest?.(data)});
    document.getElementById('revision-keep-draft')?.addEventListener('click',()=>{hideModal();onKeepDraft?.(data)});
    document.getElementById('revision-retry')?.addEventListener('click',()=>{hideModal();void onRetry?.(data)});
    document.getElementById('revision-close')?.addEventListener('click',hideModal);
  }

  function itemById(id){
    return state.items.find(item=>safeInteger(item.id)===safeInteger(id))||null;
  }

  function closeChapterEditors(exceptId=null){
    document.querySelectorAll('.mc-chapter-editor').forEach(editor=>{
      if(exceptId!==null&&editor.id==='mistake-chapter-editor-'+exceptId)return;
      editor.hidden=true;
      const id=editor.id.replace('mistake-chapter-editor-','');
      document.querySelector(`.js-chapter-toggle[data-id="${id}"]`)
        ?.setAttribute('aria-expanded','false');
    });
  }

  function fillChapterSelect(select,chapters,item){
    select.replaceChildren(new Option('未归类',''));
    chapters.forEach(chapter=>select.add(new Option(
      String(chapter.name??''),String(safeInteger(chapter.id,''))
    )));
    select.value=item.chapter_id==null?'':String(safeInteger(item.chapter_id,''));
    select.dataset.originalValue=select.value;
    select.disabled=false;
    window.Cuoti.ui.selects?.sync(select);
  }

  async function toggleChapter(id,button){
    const item=itemById(id);
    const editor=document.getElementById('mistake-chapter-editor-'+id);
    if(!item||!editor)return;
    const opening=editor.hidden;
    closeChapterEditors(opening?id:null);
    editor.hidden=!opening;
    button.setAttribute('aria-expanded',String(opening));
    if(!opening)return;

    const select=editor.querySelector('.mc-chapter-select');
    const save=editor.querySelector('.js-chapter-save');
    const status=editor.querySelector('.mc-chapter-save-status');
    select.disabled=true;save.disabled=true;status.className='mc-chapter-save-status';
    status.textContent='正在读取章节...';
    const subjectId=safeInteger(item.subject_id);
    try{
      let chapters=chapterCache.get(subjectId);
      if(!chapters){
        chapters=await apiAdapter.chapters(subjectId);
        if(!Array.isArray(chapters))throw new Error('章节数据格式无效');
        chapterCache.set(subjectId,chapters);
      }
      if(document.getElementById('mistake-chapter-editor-'+id)!==editor||editor.hidden)return;
      fillChapterSelect(select,chapters,item);
      save.disabled=false;status.textContent='';
      const trigger=select.closest('.select-control')?.querySelector('.select-trigger');
      if(trigger)trigger.focus();else select.focus();
    }catch(error){
      if(editor.hidden)return;
      status.className='mc-chapter-save-status error';
      status.textContent='章节读取失败：'+error.message;
    }
  }

  function cancelChapter(id){
    const editor=document.getElementById('mistake-chapter-editor-'+id);
    if(!editor)return;
    const select=editor.querySelector('.mc-chapter-select');
    if(select){select.value=select.dataset.originalValue||'';window.Cuoti.ui.selects?.sync(select)}
    editor.hidden=true;
    const toggle=document.querySelector(`.js-chapter-toggle[data-id="${id}"]`);
    toggle?.setAttribute('aria-expanded','false');toggle?.focus();
  }

  async function saveChapter(id){
    const item=itemById(id);
    const editor=document.getElementById('mistake-chapter-editor-'+id);
    if(!item?.public_id||!editor)return;
    const select=editor.querySelector('.mc-chapter-select');
    const status=editor.querySelector('.mc-chapter-save-status');
    const controls=[...editor.querySelectorAll('select,button')];
    const selectedValue=select.value;
    const chapterId=selectedValue?safeInteger(selectedValue):null;
    if(selectedValue===(item.chapter_id==null?'':String(safeInteger(item.chapter_id,'')))){
      cancelChapter(id);return;
    }
    controls.forEach(control=>control.disabled=true);
    status.className='mc-chapter-save-status saving';status.textContent='保存中...';
    try{
      const result=await apiAdapter.updateChapter(
        item.public_id,safeInteger(item.revision),chapterId
      );
      item.chapter_id=result.chapter_id??null;
      item.chapter=result.chapter??null;
      item.revision=safeInteger(result.revision,item.revision);
      const toggle=document.querySelector(`.js-chapter-toggle[data-id="${id}"]`);
      const label=toggle?.querySelector('.mc-chapter-label');
      if(label)label.textContent=item.chapter||'未归类';
      editor.hidden=true;toggle?.setAttribute('aria-expanded','false');
      showToast('章节已修改','success');
      await window.Cuoti.mistakes.controller.load();
    }catch(error){
      status.className='mc-chapter-save-status error';
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT'){
        status.textContent='章节保存冲突，请确认最新内容。';
        showRevisionConflict(error,{
          onLoadLatest:()=>window.Cuoti.mistakes.controller.load(),
          onKeepDraft:data=>{
            item.revision=data.currentRevision;
            status.textContent='已保留当前章节选择，可再次保存';
          },
          onRetry:async data=>{
            item.revision=data.currentRevision;await saveChapter(id);
          }
        });
      }else status.textContent='保存失败：'+error.message;
    }finally{
      controls.forEach(control=>control.disabled=false);
    }
  }

  function toggleNote(id,button){
    const editor=document.getElementById('mistake-note-editor-'+id);
    const item=itemById(id);
    if(!editor||!item)return;
    const willOpen=editor.hidden;
    editor.hidden=!willOpen;
    button.setAttribute('aria-expanded',String(willOpen));
    if(willOpen){
      const input=editor.querySelector('textarea');
      input.value=String(item.note||'');
      input.dataset.originalValue=input.value;
      input.dispatchEvent(new Event('input'));
      editor.querySelector('.mc-note-save-status').textContent='';
      editor.querySelector('.mc-note-conflict').hidden=true;
      input.focus();
    }
  }

  function cancelNote(id){
    const editor=document.getElementById('mistake-note-editor-'+id);
    if(!editor)return;
    const input=editor.querySelector('textarea');
    input.value=input.dataset.originalValue||'';
    input.dispatchEvent(new Event('input'));
    editor.hidden=true;
    const toggle=document.querySelector(`.js-note-toggle[data-id="${id}"]`);
    toggle?.setAttribute('aria-expanded','false');
    toggle?.focus();
  }

  function resolveNoteConflict(id,loadLatest){
    const item=itemById(id);
    const editor=document.getElementById('mistake-note-editor-'+id);
    if(!item||!editor)return;
    const conflict=editor.querySelector('.mc-note-conflict');
    const latestRevision=safeInteger(conflict.dataset.latestRevision,item.revision);
    item.revision=latestRevision;
    if(loadLatest){
      const latest=conflict.dataset.latestNote||'';
      item.note=latest;
      const input=editor.querySelector('textarea');
      input.value=latest;input.dataset.originalValue=latest;
      input.dispatchEvent(new Event('input'));
    }
    conflict.hidden=true;
    editor.querySelector('.mc-note-save-status').textContent=loadLatest
      ?'已载入最新备注':'草稿已保留，可再次保存';
  }

  async function saveNote(id){
    const item=itemById(id);
    const editor=document.getElementById('mistake-note-editor-'+id);
    if(!item||!editor||!item.public_id)return;
    const input=editor.querySelector('textarea');
    const status=editor.querySelector('.mc-note-save-status');
    const conflict=editor.querySelector('.mc-note-conflict');
    const buttons=[...editor.querySelectorAll('button')];
    buttons.forEach(control=>control.disabled=true);
    status.className='mc-note-save-status saving';status.textContent='保存中...';
    conflict.hidden=true;
    try{
      const result=await apiAdapter.updateNote(
        item.public_id,safeInteger(item.revision),input.value
      );
      item.note=result.note||'';item.revision=safeInteger(result.revision,item.revision);
      input.value=item.note;input.dataset.originalValue=item.note;
      input.dispatchEvent(new Event('input'));
      const display=document.getElementById('mistake-note-display-'+id);
      if(display){
        display.hidden=!item.note;
        const text=display.querySelector('.mc-note-text');
        if(text){
          text.textContent=item.note;
          if(item.note)void renderMathNodes(text,{force:true});
        }
      }
      const toggle=document.querySelector(`.js-note-toggle[data-id="${id}"]`);
      const label=item.note?'编辑备注':'添加备注';
      if(toggle){
        toggle.title=label;
        toggle.setAttribute('aria-label',label);
        toggle.setAttribute('aria-expanded','false');
      }
      status.className='mc-note-save-status saved';status.textContent='已保存';
      editor.hidden=true;
    }catch(error){
      status.className='mc-note-save-status error';
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT'){
        status.textContent='保存冲突，请选择如何处理。';
        conflict.dataset.latestRevision=String(safeInteger(error.data.current_revision,item.revision));
        conflict.dataset.latestNote=String(
          error.data.current_note
          ?? error.data.latest?.note
          ?? ''
        );
        conflict.hidden=false;
      }else{
        status.textContent='保存失败：'+error.message;
      }
    }finally{
      buttons.forEach(control=>control.disabled=false);
    }
  }

  async function toggleStatus(id){
    const item=itemById(id);
    if(!item?.public_id)return;
    try{
      const result=await apiAdapter.toggle(item.public_id,safeInteger(item.revision));
      item.revision=safeInteger(result?.revision,item.revision);
      await window.Cuoti.mistakes.controller.load();
    }catch(error){
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT'){
        showRevisionConflict(error,{onLoadLatest:data=>{
          if(data.latest){Object.assign(item,data.latest);item.reasons=data.latest.reasons||[]}
          void window.Cuoti.mistakes.controller.load();
        },onRetry:async data=>{
          item.revision=data.currentRevision;
          try{await apiAdapter.toggle(item.public_id,item.revision);await window.Cuoti.mistakes.controller.load()}
          catch(retryError){showRevisionConflict(retryError,{onRetry:async()=>toggleStatus(id)})}
        }});
      }else showModal('操作失败',error.message,'error');
    }
  }

  function toggleReason(id,button){
    const editor=document.getElementById('mistake-reason-editor-'+id);
    if(!editor)return;
    const willOpen=editor.hidden;
    editor.hidden=!willOpen;
    button.setAttribute('aria-expanded',String(willOpen));
    if(willOpen)editor.querySelector('.mc-reason-choice')?.focus();
  }

  async function updateReason(id,button){
    const item=itemById(id);
    const editor=document.getElementById('mistake-reason-editor-'+id);
    if(!editor||!item?.public_id)return;
    const choices=[...editor.querySelectorAll('.mc-reason-choice')];
    const wasSelected=button.classList.contains('on');
    button.classList.toggle('on',!wasSelected);
    button.setAttribute('aria-pressed',String(!wasSelected));
    const selected=choices.filter(choice=>choice.classList.contains('on'))
      .map(choice=>choice.dataset.reason);
    const status=editor.querySelector('.mc-reason-save-status');
    choices.forEach(choice=>choice.disabled=true);
    status.className='mc-reason-save-status saving';status.textContent='保存中...';
    try{
      const result=await apiAdapter.updateReasons(
        item.public_id,safeInteger(item.revision),selected
      );
      if(result.error)throw new Error(result.error);
      item.reasons=result.reasons||selected;
      item.revision=safeInteger(result.revision,item.revision);
      const badges=document.getElementById('mistake-reason-badges-'+id);
      if(badges){
        badges.innerHTML=selected.map(reason=>
          `<span class="badge badge-r">${escapeHTML(reason)}</span>`
        ).join('');
      }
      status.className='mc-reason-save-status saved';status.textContent='已保存';
    }catch(error){
      button.classList.toggle('on',wasSelected);
      button.setAttribute('aria-pressed',String(wasSelected));
      status.className='mc-reason-save-status error';
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT'){
        showRevisionConflict(error,{onLoadLatest:data=>{
          if(data.latest){item.revision=data.currentRevision;item.reasons=data.latest.reasons||[];choices.forEach(choice=>{const on=item.reasons.includes(choice.dataset.reason);choice.classList.toggle('on',on);choice.setAttribute('aria-pressed',String(on))})}
        },onKeepDraft:()=>{status.textContent='草稿已保留，请再次点击错因保存'},onRetry:async data=>{
          item.revision=data.currentRevision;
          try{const result=await apiAdapter.updateReasons(item.public_id,item.revision,selected);item.revision=safeInteger(result.revision,item.revision);item.reasons=result.reasons||selected;status.textContent='已保存'}catch(retryError){showRevisionConflict(retryError,{onRetry:()=>updateReason(id,button)})}
        }});
      }else status.textContent='保存失败：'+error.message;
    }finally{
      choices.forEach(choice=>choice.disabled=false);
    }
  }

  async function remove(id){
    const item=itemById(id);
    if(!item?.public_id)return;
    showModal('移入回收站','确定要将这道错题移入回收站吗？移入回收站，可恢复。','error',
      '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
      '<button class="btn btn-d btn-sm" id="modal-confirm-btn">确定删除</button>');
    document.getElementById('modal-confirm-btn').onclick=async()=>{
      hideModal();
      try{
        await apiAdapter.remove(item.public_id,safeInteger(item.revision));
        state.selectedPublicIds.delete(String(item.public_id));
        window.Cuoti.mistakes.exports.updateBar();
        await window.Cuoti.mistakes.controller.load();
      }catch(error){
        if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT')showRevisionConflict(error,{onRetry:async data=>{item.revision=data.currentRevision;await remove(id)}});
        else showModal('删除失败',error.message,'error');
      }
    };
  }

  async function toggleAttachments(id,button){
    const item=itemById(id);if(!item?.public_id)return;
    const panel=document.getElementById('mistake-attachments-'+id);if(!panel)return;
    const opening=panel.hidden;panel.hidden=!opening;
    button.setAttribute('aria-expanded',String(opening));
    if(!opening)return;
    const status=panel.querySelector('.mc-attachment-status');
    status.textContent='正在读取附件...';
    try{
      const payload=await apiAdapter.attachments(item.public_id);
      panel.querySelector('.mc-attachment-list').innerHTML=window.Cuoti.mistakes.render.attachmentList(payload.items||[]);
      status.textContent='';panel.dataset.loaded='1';
    }catch(error){status.textContent='附件读取失败：'+error.message}
  }

  async function uploadAttachment(id,input,fileOverride=null){
    // Keep the selected File object outside the input element. Browsers clear
    // the input after the first request; retaining the object lets a user
    // safely re-apply the same upload after resolving a revision conflict.
    const item=itemById(id),file=fileOverride||input.files?.[0];
    if(!item?.public_id||!file)return;
    const panel=document.getElementById('mistake-attachments-'+id);
    const status=panel?.querySelector('.mc-attachment-status');
    if(status)status.textContent='正在上传附件...';input.disabled=true;
    try{
      const result=await apiAdapter.uploadAttachment(item.public_id,safeInteger(item.revision),file);
      item.revision=safeInteger(result.revision,item.revision);
      const payload=await apiAdapter.attachments(item.public_id);
      panel.querySelector('.mc-attachment-list').innerHTML=window.Cuoti.mistakes.render.attachmentList(payload.items||[]);
      if(status)status.textContent='附件已上传';
    }catch(error){
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT')showRevisionConflict(error,{onLoadLatest:data=>{if(data.latest)item.revision=data.currentRevision},onRetry:async data=>{item.revision=data.currentRevision;await uploadAttachment(id,input,file)}});
      else if(status)status.textContent='附件上传失败：'+error.message;
    }
    finally{input.disabled=false;input.value=''}
  }

  async function restore(id){
    const item=itemById(id);if(!item?.public_id)return;
    try{
      await apiAdapter.restore(item.public_id,safeInteger(item.revision));
      showToast('错题已恢复','success');
      if(state.view==='trash'){
        await window.Cuoti.mistakes.controller.discardTrashItem(id);
      }else{
        await window.Cuoti.mistakes.controller.load();
      }
    }catch(error){
      if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT')showRevisionConflict(error,{onRetry:async data=>{item.revision=data.currentRevision;await restore(id)}});
      else showModal('恢复失败',error.message,'error')
    }
  }

  async function purge(id){
    const item=itemById(id);if(!item?.public_id)return;
    showModal('永久删除','永久删除后正文、复习记录和附件均不可恢复。确定继续吗？','error',
      '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
      '<button class="btn btn-d btn-sm" id="modal-confirm-btn">永久删除</button>');
    document.getElementById('modal-confirm-btn').onclick=async()=>{
      hideModal();
      try{
        await apiAdapter.purge(item.public_id,safeInteger(item.revision));
        showToast('错题已永久删除','success');
        if(state.view==='trash'){
          await window.Cuoti.mistakes.controller.discardTrashItem(id);
        }
      }catch(error){
        if(error?.status===409&&error?.data?.code==='REVISION_CONFLICT')showRevisionConflict(error,{onRetry:async data=>{item.revision=data.currentRevision;await purge(id)}});
        else showModal('永久删除失败',error.message,'error')
      }
    };
  }

  window.Cuoti.mistakes.editors=Object.freeze({
    toggleChapter,cancelChapter,saveChapter,
    itemById,toggleNote,cancelNote,resolveNoteConflict,saveNote,
    toggleStatus,toggleReason,updateReason,remove,toggleAttachments,uploadAttachment,
    restore,purge,showRevisionConflict
  });
})();
