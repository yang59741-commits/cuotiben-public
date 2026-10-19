(function initDailyContent(){
  'use strict';

  let value={};
  let request=null;
  let tips=[];
  let nextBeforeId=null;
  let total=0;
  let editing=null;
  let managerMounted=false;

  function html(){
    const tip=value.tip?escapeHTML(value.tip||''):'尚未录入每日提示';
    return `<div class="dc"><div class="dh">${icon('bookOpen')}每日术语</div><div class="dt">${escapeHTML(value.word||'')}</div><div class="dd">${escapeHTML(value.word_desc||'')}</div></div>
      <div class="dc"><div class="dh">${icon('quote')}每日一句</div><div class="dd daily-quote">${escapeHTML(value.quote||'')}</div></div>
      <div class="dc"><div class="dh">${icon('lightbulb')}每日提示</div><div class="dd${value.tip?' math':' daily-tip-empty'}">${tip}</div></div>`;
  }

  function render(){
    for(const id of ['daily-box','daily-box2']){
      const root=document.getElementById(id);
      if(root){root.innerHTML=html();void renderMath(root)}
    }
  }

  async function load({force=false}={}){
    if(force)request=null;
    if(request)return request;
    request=api('/api/daily').then(payload=>{
      value=payload||{};
      render();
      return value;
    }).catch(error=>{
      console.warn('Daily content could not be loaded:',error.message);
      value={word:'',word_desc:'',quote:'',tip:''};
      render();
      return value;
    });
    return request;
  }

  function managerElements(){
    return {
      content:document.getElementById('daily-tip-content'),
      save:document.getElementById('daily-tip-save'),
      cancel:document.getElementById('daily-tip-cancel'),
      status:document.getElementById('daily-tip-status'),
      list:document.getElementById('daily-tip-list'),
      count:document.getElementById('daily-tip-count'),
      more:document.getElementById('daily-tip-more')
    };
  }

  function setManagerBusy(busy,message=''){
    const elements=managerElements();
    if(elements.save)elements.save.disabled=busy;
    if(elements.status)elements.status.textContent=message;
  }

  function resetEditor(){
    editing=null;
    const {content,save,cancel}=managerElements();
    if(content)content.value='';
    window.Cuoti.ui.editor?.refreshPreview?.('daily-tip-content');
    if(save)save.innerHTML=`${icon('plus')}<span>保存提示</span>`;
    if(cancel)cancel.hidden=true;
  }

  function renderManager(){
    const {list,count,more}=managerElements();
    if(!list)return;
    if(count)count.textContent=`${total} 条`;
    if(!tips.length){
      list.innerHTML='<div class="daily-tip-list-empty">还没有每日提示，录入后会参与非真题 PDF 组卷。</div>';
    }else{
      list.innerHTML=tips.map(item=>`<article class="daily-tip-item" data-tip-id="${escapeHTML(item.public_id)}">
        <div class="daily-tip-item-content math">${escapeHTML(item.content)}</div>
        <div class="daily-tip-item-actions">
          <button type="button" class="btn btn-out btn-xs" data-daily-tip-edit aria-label="修改每日提示">${icon('edit')}修改</button>
          <button type="button" class="btn btn-d btn-xs" data-daily-tip-delete aria-label="删除每日提示">${icon('trash')}删除</button>
        </div>
      </article>`).join('');
      void renderMath(list);
    }
    if(more){more.hidden=!nextBeforeId;more.disabled=false}
  }

  async function loadTips({append=false}={}){
    const params=new URLSearchParams({limit:'20'});
    if(append&&nextBeforeId)params.set('before_id',String(nextBeforeId));
    const page=await api(`/api/v2/daily-tips?${params}`);
    const incoming=Array.isArray(page.items)?page.items:[];
    tips=append?[...tips,...incoming]:incoming;
    total=Number(page.total)||0;
    nextBeforeId=page.next_before_id==null?null:Number(page.next_before_id);
    renderManager();
  }

  function beginEdit(item){
    editing=item;
    const {content,save,cancel}=managerElements();
    content.value=item.content;
    window.Cuoti.ui.editor?.refreshPreview?.('daily-tip-content');
    save.innerHTML=`${icon('check')}<span>保存修改</span>`;
    cancel.hidden=false;
    content.focus();
    content.scrollIntoView({block:'center',behavior:'smooth'});
  }

  async function saveTip(){
    const {content}=managerElements();
    const text=String(content?.value||'').trim();
    if(!text){showFieldError(content,'请先输入每日提示内容');return}
    setManagerBusy(true,editing?'正在保存修改…':'正在保存…');
    try{
      if(editing){
        await api(`/api/v2/daily-tips/${encodeURIComponent(editing.public_id)}`,{
          method:'PATCH',body:JSON.stringify({content:text,revision:editing.revision})
        });
      }else{
        await api('/api/v2/daily-tips',{method:'POST',body:JSON.stringify({content:text})});
      }
      showToast(editing?'每日提示已更新':'每日提示已保存','success');
      resetEditor();
      await Promise.all([loadTips(),load({force:true})]);
    }catch(error){
      if(error?.data?.code==='REVISION_CONFLICT'){
        showModal('内容已变化','这条每日提示已在别处修改，列表将刷新；你的输入仍保留。','info');
        editing=null;
        await loadTips();
      }else{
        showModal('保存失败',error.message,'error');
      }
    }finally{setManagerBusy(false,'')}
  }

  function confirmDelete(item){
    showModal(
      '删除每日提示',
      '删除后不会再出现在每日卡片或之后生成的 PDF 中。',
      'warning',
      '<button type="button" class="btn btn-out btn-sm" data-action="hide-modal">取消</button><button type="button" class="btn btn-d btn-sm" id="daily-tip-delete-confirm">删除</button>'
    );
    document.getElementById('daily-tip-delete-confirm')?.addEventListener('click',async event=>{
      event.currentTarget.disabled=true;
      try{
        await api(`/api/v2/daily-tips/${encodeURIComponent(item.public_id)}`,{
          method:'DELETE',body:JSON.stringify({revision:item.revision})
        });
        hideModal();
        if(editing?.public_id===item.public_id)resetEditor();
        showToast('每日提示已删除','success');
        await Promise.all([loadTips(),load({force:true})]);
      }catch(error){
        hideModal();
        showModal('删除失败',error.message,'error');
        await loadTips();
      }
    },{once:true});
  }

  function bindManager(){
    const elements=managerElements();
    if(!elements.list||elements.list.dataset.bound==='true')return;
    elements.list.dataset.bound='true';
    elements.save?.addEventListener('click',()=>void saveTip());
    elements.cancel?.addEventListener('click',resetEditor);
    elements.more?.addEventListener('click',async()=>{
      elements.more.disabled=true;
      try{await loadTips({append:true})}
      catch(error){showModal('加载失败',error.message,'error');elements.more.disabled=false}
    });
    elements.list.addEventListener('click',event=>{
      const itemRoot=event.target.closest('[data-tip-id]');
      if(!itemRoot)return;
      const item=tips.find(candidate=>candidate.public_id===itemRoot.dataset.tipId);
      if(!item)return;
      if(event.target.closest('[data-daily-tip-edit]'))beginEdit(item);
      if(event.target.closest('[data-daily-tip-delete]'))confirmDelete(item);
    });
    document.querySelectorAll('[data-daily-latex]').forEach(button=>{
      button.addEventListener('click',()=>{
        const insertion=button.dataset.dailyLatex==='display-math'
          ?{before:'\\[\n',after:'\n\\]'}:{before:'$',after:'$'};
        window.Cuoti.ui.editor.insertAtSelection(elements.content,insertion);
      });
    });
  }

  async function mountManager(){
    managerMounted=true;
    bindManager();
    setManagerBusy(true,'正在加载…');
    try{await loadTips()}
    catch(error){showModal('每日提示加载失败',error.message,'error')}
    finally{if(managerMounted)setManagerBusy(false,'')}
  }

  function unmountManager(){managerMounted=false}
  function refreshManager(){return mountManager()}

  window.Cuoti.daily=Object.freeze({
    load,render,html,current:()=>Object.freeze({...value}),
    mountManager,unmountManager,refreshManager
  });
  window.dailyHTML=html;
  window.loadDaily=load;
  window.renderDaily=render;
})();
