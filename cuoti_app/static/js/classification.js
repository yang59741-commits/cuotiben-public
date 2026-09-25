(function initClassification(){
  'use strict';
  window.Cuoti=window.Cuoti||{};
  const labels={auto:'自动分类',manual:'人工确认',stale:'待复核',needs_review:'待确认',unclassified:'未分类',historical_unknown:'未记录分类'};
  let revision=0;
  const esc=escapeHTML;
  const split=value=>String(value||'').split(/[，,\n]/).map(v=>v.trim()).filter(Boolean);
  function status(message){const node=document.getElementById('classification-message');if(node)node.textContent=message;}
  function run(task){return async event=>{event?.preventDefault();const button=event?.submitter||event?.currentTarget;if(button)button.disabled=true;try{await task(event)}catch(error){status(error.message)}finally{if(button?.isConnected)button.disabled=false}};}
  function modal(title,body){
    showModal(title,'','info');
    document.querySelector('#modal .modal-box')?.classList.add('modal-box-wide');
    document.getElementById('modal-body').innerHTML=body+'<p id="classification-message" role="status"></p>';
  }
  function options(types,selected=''){
    return '<option value="">请选择题型</option>'+types.map(t=>`<option value="${esc(t.code)}"${t.code===selected?' selected':''}>${esc(t.name)}</option>`).join('');
  }
  function summary(value){
    const typed=value||{};
    return `<span class="classification-status">${esc(labels[typed.status]||'未分类')}</span>${typed.type_name?` <strong>${esc(typed.type_name)}</strong>`:''}`;
  }
  function inline(item){
    return `<div class="classification-inline">${summary(item.classification)} <button type="button" class="btn btn-out btn-xs" data-classification-id="${esc(item.mistake_public_id||item.publicId||'')}" title="调整分类" aria-label="调整分类">${icon('edit')}</button></div>`;
  }
  async function open(publicId,preferred=''){
    const token=++revision;
    const typed=await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`);
    const {items:types}=await api('/api/v2/question-types?subject='+encodeURIComponent(typed.subject||''));
    if(token!==revision)return;
    const suggestion=typed.suggestion;
    const proposed=suggestion?.payload||{};
    const value=typed.revision?typed:proposed;
    modal('错题分类',`<form id="classification-form" class="classification-form">
      <div>${summary(typed)}${typed.locked?' · 已锁定人工分类':''}</div>
      ${suggestion?`<div class="classification-notice">${esc(suggestion.reason)}${proposed.proposed_type?`<p>建议新类：${esc(proposed.proposed_type.name||'')}<br>${esc(proposed.proposed_type.definition||'')}</p>`:''}</div>`:''}
      <label>主要题型<select id="classification-type">${options(types,preferred||value.primary_type_code||proposed.primary_type_code)}</select></label>
      <label>知识点<input id="classification-knowledge" value="${esc((value.knowledge_points||[]).join('，'))}" placeholder="最多 5 项，以逗号分隔"></label>
      <label>方法<input id="classification-methods" value="${esc((value.methods||[]).join('，'))}" placeholder="最多 3 项，以逗号分隔"></label>
      <div class="classification-actions"><button class="btn btn-p" type="submit">${icon('check')}保存并锁定</button>
      ${typed.locked?'<button class="btn btn-out" type="button" id="classification-unlock">恢复自动分类</button>':''}
      ${suggestion?'<button class="btn btn-out" type="button" id="classification-reject">拒绝建议</button>':''}
      ${proposed.proposed_type?'<button class="btn btn-out" type="button" id="classification-create">创建建议题型</button>':''}</div></form>`);
    document.getElementById('classification-form').addEventListener('submit',run(async()=>{
      await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`,{method:'PUT',body:JSON.stringify({
        revision:typed.revision,primary_type_code:document.getElementById('classification-type').value,
        knowledge_points:split(document.getElementById('classification-knowledge').value),methods:split(document.getElementById('classification-methods').value)
      })});
      window.dispatchEvent(new Event('cuoti:classification-changed'));await open(publicId);
    }));
    document.getElementById('classification-unlock')?.addEventListener('click',run(async()=>{
      await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`,{method:'PUT',body:JSON.stringify({revision:typed.revision,action:'unlock'})});await open(publicId);
    }));
    document.getElementById('classification-reject')?.addEventListener('click',run(async()=>{
      await api(`/api/v2/classification-suggestions/${suggestion.id}/resolve`,{method:'POST',body:JSON.stringify({revision:typed.revision,action:'reject'})});await open(publicId);
    }));
    document.getElementById('classification-create')?.addEventListener('click',run(()=>editType({...proposed.proposed_type,subject:typed.subject},code=>open(publicId,code))));
  }
  async function editType(value={},after=null){
    const {items:types}=await api('/api/v2/question-types?all=1');
    const subjects=await api('/api/subjects');
    const subject=value.subject||subjects[0]?.name||'';
    const chapters=await api('/api/chapters?subject_id='+encodeURIComponent(subjects.find(s=>s.name===subject)?.id||''));
    modal(value.code?'编辑题型':'新增题型',`<form id="type-form" class="classification-form">
      <label>科目<select id="type-subject"${value.code?' disabled':''}>${subjects.map(s=>`<option${s.name===subject?' selected':''}>${esc(s.name)}</option>`).join('')}</select></label>
      <label>名称<input id="type-name" maxlength="60" required value="${esc(value.name||'')}"></label>
      <label>定义<textarea id="type-definition" maxlength="500" required>${esc(value.definition||'')}</textarea></label>
      <label>适用章节<select id="type-chapters" multiple size="5">${chapters.map(c=>`<option${(value.chapters||[]).includes(c.name)?' selected':''}>${esc(c.name)}</option>`).join('')}</select></label>
      <label>别名<input id="type-aliases" value="${esc((value.aliases||[]).join('，'))}" placeholder="以逗号分隔"></label>
      <label class="classification-check"><input type="checkbox" id="type-active"${value.active!==0?' checked':''}>启用</label>
      <div class="classification-actions"><button class="btn btn-p" type="submit">${icon('check')}保存题型</button>
      ${value.code?`<select id="type-merge">${options(types.filter(t=>t.code!==value.code&&t.subject===subject&&t.active))}</select><button class="btn btn-out" type="button" id="type-merge-button">合并到所选题型</button>`:''}</div></form>`);
    document.getElementById('type-subject').addEventListener('change',run(async event=>{
      const list=await api('/api/chapters?subject_id='+subjects.find(s=>s.name===event.target.value).id);
      document.getElementById('type-chapters').innerHTML=list.map(c=>`<option>${esc(c.name)}</option>`).join('');
    }));
    document.getElementById('type-form').addEventListener('submit',run(async()=>{
      const result=await api('/api/v2/question-types'+(value.code?'/'+encodeURIComponent(value.code):''),{method:value.code?'PUT':'POST',body:JSON.stringify({
        revision:value.revision,subject:document.getElementById('type-subject').value,name:document.getElementById('type-name').value,
        definition:document.getElementById('type-definition').value,chapters:[...document.getElementById('type-chapters').selectedOptions].map(o=>o.value),
        aliases:split(document.getElementById('type-aliases').value),active:document.getElementById('type-active').checked
      })});window.dispatchEvent(new Event('cuoti:classification-changed'));if(after)await after(result.code);else await manager();
    }));
    document.getElementById('type-merge-button')?.addEventListener('click',run(async()=>{
      await api('/api/v2/question-types/'+encodeURIComponent(value.code),{method:'PUT',body:JSON.stringify({revision:value.revision,merge_into:document.getElementById('type-merge').value})});
      window.dispatchEvent(new Event('cuoti:classification-changed'));await manager();
    }));
  }
  async function manager(tab='types'){
    ++revision;
    modal('分类管理',`<div class="tabs" role="tablist" aria-label="分类管理">${[['types','题型目录'],['pending','待确认'],['backfill','历史补标']].map(([key,label])=>`<button type="button" class="tab${tab===key?' on':''}" data-class-tab="${key}" role="tab" aria-selected="${tab===key}">${label}</button>`).join('')}</div><div id="classification-manager"></div>`);
    document.querySelectorAll('[data-class-tab]').forEach(b=>b.addEventListener('click',run(()=>manager(b.dataset.classTab))));
    const root=document.getElementById('classification-manager');
    if(tab==='types'){
      const {items}=await api('/api/v2/question-types?all=1');
      if(!root.isConnected)return;
      root.innerHTML=`<button type="button" class="btn btn-out btn-sm" id="type-add">${icon('plus')}新增题型</button><div class="classification-table"><table class="tb"><thead><tr><th>科目</th><th>题型</th><th>定义</th><th>状态</th><th>操作</th></tr></thead><tbody>${items.map(t=>`<tr><td>${esc(t.subject)}</td><td>${esc(t.name)}</td><td>${esc(t.definition)}</td><td>${t.active?'启用':'停用'}</td><td><button type="button" class="btn btn-out btn-xs" data-type-edit="${esc(t.code)}" title="编辑题型" aria-label="编辑题型">${icon('edit')}</button></td></tr>`).join('')}</tbody></table></div>`;
      root.querySelector('#type-add').addEventListener('click',run(()=>editType()));
      root.querySelectorAll('[data-type-edit]').forEach(b=>b.addEventListener('click',run(()=>editType(items.find(t=>t.code===b.dataset.typeEdit)))));
    }else if(tab==='pending'){
      const {items}=await api('/api/v2/classification-suggestions');
      if(!root.isConnected)return;
      root.innerHTML=items.length?`<button type="button" class="btn btn-out btn-sm" id="classification-batch">${icon('check')}接受勾选建议</button>${items.map(item=>`<article class="classification-pending"><label class="classification-check"><input type="checkbox" value="${item.id}" data-class-pick${!item.classification.suggestion.payload.primary_type_code?' disabled':''}>${esc(item.subject)} ${esc(item.qnum||'未标号')}</label><div class="math">${problemPreviewHTML(item.content)}</div><p>${esc(item.classification.suggestion.reason)}</p>${inline({...item,mistake_public_id:item.public_id})}</article>`).join('')}`:'<p>没有待确认的分类。</p>';
      root.querySelector('#classification-batch')?.addEventListener('click',run(async()=>{
        let saved=0;const errors=[];
        for(const node of root.querySelectorAll('[data-class-pick]:checked')){
          const item=items.find(i=>String(i.id)===node.value);
          try{await api(`/api/v2/classification-suggestions/${item.id}/resolve`,{method:'POST',body:JSON.stringify({action:'accept',revision:item.classification.revision})});saved++}catch(error){errors.push(error.message)}
        }
        await manager('pending');status(`已接受 ${saved} 项${errors.length?'；'+errors.join('；'):''}`);
        window.dispatchEvent(new Event('cuoti:classification-changed'));
      }));
      void renderMath(root);
    }else{
      const subjects=await api('/api/subjects');
      if(!root.isConnected)return;
      root.innerHTML=`<form id="backfill-form" class="classification-form"><label>科目<select id="backfill-subject"><option value="">全部科目</option>${subjects.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></label><label>录入日期起<input type="date" id="backfill-from"></label><label>录入日期止<input type="date" id="backfill-to"></label><button class="btn btn-out" type="submit">预览补标范围</button></form><div id="backfill-preview"></div>`;
      root.querySelector('form').addEventListener('submit',run(async()=>{
        const scope={subject_id:root.querySelector('#backfill-subject').value,date_from:root.querySelector('#backfill-from').value,date_to:root.querySelector('#backfill-to').value};
        const preview=await api('/api/v2/mistake-classifications/backfill/preview',{method:'POST',body:JSON.stringify(scope)});
        const box=root.querySelector('#backfill-preview');
        box.innerHTML=`<p>待补标 ${preview.count} 道 · 人工锁定 ${preview.locked_count} 道 · 待确认 ${preview.pending_count} 道</p><p>预计输入约 ${preview.estimated_input_tokens} Token，实际按批次计算。</p><button type="button" class="btn btn-p" id="backfill-submit"${preview.count?'':' disabled'}>确认并开始补标</button>`;
        box.querySelector('button').addEventListener('click',run(async()=>{
          const result=await api('/api/v2/mistake-classifications/backfill',{method:'POST',body:JSON.stringify({scope,fingerprint:preview.fingerprint,request_id:crypto.randomUUID()})});
          status(`补标任务已提交：${result.job_id}`);
          document.getElementById('task-panel-toggle')?.click();
        }));
      }));
    }
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-classification-id]');
    if(button)open(button.dataset.classificationId).catch(error=>showModal('分类读取失败',error.message,'error'));
  });
  window.Cuoti.classification=Object.freeze({open,manager,inline,summary});
})();
