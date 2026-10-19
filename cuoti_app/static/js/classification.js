/* global modalRevision */
(function initClassification(){
  'use strict';
  window.Cuoti=window.Cuoti||{};
  const labels={auto:'自动分类',manual:'人工确认',stale:'待复核',needs_review:'待确认',unclassified:'未分类',historical_unknown:'未记录分类'};
  let revision=0;
  const esc=escapeHTML;
  const split=value=>String(value||'').split(/[，,\n]/).map(v=>v.trim()).filter(Boolean);
  const methodKey=value=>String(value||'').normalize('NFKC').trim().replace(/\s+/g,' ').toLowerCase();
  function methodByName(methods,name){return methods.find(item=>[item.name,...item.aliases].some(alias=>methodKey(alias)===methodKey(name)));}
  function methodItems(value){return Array.isArray(value.method_annotations)?value.method_annotations.filter(item=>item&&typeof item==='object'):(Array.isArray(value.methods)?value.methods:[]).map(name=>({name,role:'unspecified'}));}
  function suggestionDetails(value,types,methods){
    const type=types.find(item=>item.code===value.primary_type_code)?.name||value.proposed_type?.name||'题型待归类';
    const roles={primary:'主要',auxiliary:'辅助',unspecified:'未区分'};
    const names=methodItems(value).map(item=>`${roles[item.role]||'未区分'}：${methods.find(method=>method.code===item.code)?.name||item.name||'方法待归类'}`);
    return `<p class="math">建议题型：${esc(type)}</p><p class="math">建议方法：${esc(names.join('；')||'未标注')}</p>${value.method_basis?`<p class="math">选法依据：${esc(value.method_basis)}</p>`:''}`;
  }
  function methodFields(value,methods){
    const annotations=methodItems(value);
    return `<fieldset class="classification-methods"><legend>解题方法</legend><p class="daily-drawer-help">一个主要方法，最多两个同一路径的辅助方法。可按名称或别名查找；不能确定作用时保留“待归类”。</p><datalist id="classification-method-catalog">${methods.flatMap(item=>[item.name,...item.aliases].map(name=>`<option value="${esc(name)}">${esc(item.name+' · '+item.definition)}</option>`)).join('')}</datalist>${Array.from({length:3},(_,index)=>{
      const item=annotations[index]||{};
      const name=item.name||methods.find(method=>method.code===item.code)?.name||item.code||'';
      const definition=methodByName(methods,name)?.definition||'';
      return `<div class="classification-method-row"><label>方法 ${index+1}<input data-method-name list="classification-method-catalog" maxlength="40" value="${esc(name)}" placeholder="输入或选择方法"></label><label>作用<select data-method-role>${[['unspecified','未区分 / 待归类'],['primary','主要方法'],['auxiliary','辅助方法']].map(([role,label])=>`<option value="${role}"${(item.role||'unspecified')===role?' selected':''}>${label}</option>`).join('')}</select></label><small class="math" data-method-definition>${esc(definition)}</small></div>`;
    }).join('')}<p class="daily-drawer-help">目录外名称可保留为“待归类”，不会作为已确认目录方法参与专项筛选。</p></fieldset>`;
  }
  function status(message){const node=document.getElementById('classification-message');if(node)node.textContent=message;}
  function run(task){return async event=>{event?.preventDefault();const button=event?.submitter||event?.currentTarget;const epoch=modalRevision;if(button)button.disabled=true;try{await task(event)}catch(error){if(epoch===modalRevision)status(error.message)}finally{if(button?.isConnected)button.disabled=false}};}
  function modal(title,body){
    showModal(title,'','info','<button type="button" class="btn btn-out btn-sm" data-action="hide-modal">关闭</button>');
    document.querySelector('#modal .modal-box')?.classList.add('modal-box-wide');
    document.getElementById('modal-body').innerHTML=body+'<p id="classification-message" role="status"></p>';
    void renderMath(document.getElementById('modal-body'));
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
  async function open(publicId,preferred='',useSuggestion=false){
    const token=++revision;
    const originHash=window.location.hash,originModal=modalRevision;
    const typed=await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`);
    const {items:types}=await api('/api/v2/question-types?subject='+encodeURIComponent(typed.subject||''));
    const {items:methods}=await api('/api/v2/solution-methods?subject='+encodeURIComponent(typed.subject||''));
    if(token!==revision||originHash!==window.location.hash||originModal!==modalRevision)return;
    const suggestion=typed.suggestion;
    const proposed=suggestion?.payload||{};
    const draft=Boolean(suggestion?.is_current&&(useSuggestion||!typed.locked));
    const value=draft?proposed:typed;
    modal('方法与题型确认',`<form id="classification-form" class="classification-form">
      ${typed.question?`<div class="classification-question"><strong>${esc([typed.subject,typed.chapter,typed.qnum].filter(Boolean).join(' · '))}</strong>${problemPreviewHTML(typed.question)}</div>`:''}
      <div>${summary(typed)}${typed.locked?' · 已锁定人工分类':''}</div>
      <p class="daily-drawer-help">${draft?'建议已填入下方，核对后保存。':'可调整方法与题型。'}关闭不保存；已有复习批次保持原标注。</p>
      ${suggestion?`<div class="classification-notice${suggestion.is_current?'':' stale'}"><strong>${suggestion.is_current?'待确认建议':'建议已过期，请重新分析后再使用'}</strong><p>${esc(suggestion.reason)}</p>${draft?(proposed.method_basis?`<p class="math">选法依据：${esc(proposed.method_basis)}</p>`:''):suggestionDetails(proposed,types,methods)}${suggestion.is_current&&!draft?'<button class="btn btn-out btn-sm" type="button" id="classification-use-suggestion">用建议填入</button>':''}</div>`:''}
      ${methodFields(value,methods)}
      <details class="classification-extra"${!(preferred||value.primary_type_code)?' open':''}><summary>题型与知识点 · ${esc(types.find(item=>item.code===(preferred||value.primary_type_code))?.name||'请选择题型')}</summary>
      <label>主要题型<select id="classification-type">${options(types,preferred||value.primary_type_code)}</select></label>
      <label>知识点<input id="classification-knowledge" value="${esc((Array.isArray(value.knowledge_points)?value.knowledge_points:[]).join('，'))}" placeholder="最多 5 项，以逗号分隔"></label></details>
      <div class="classification-actions"><button class="btn btn-p" type="submit">${icon('check')}确认并保存</button>
      ${typed.locked?'<button class="btn btn-out" type="button" id="classification-unlock">恢复自动分类</button>':''}
      ${suggestion?'<button class="btn btn-out" type="button" id="classification-reject">拒绝建议</button>':''}
      ${suggestion?.is_current&&proposed.proposed_type?'<button class="btn btn-out" type="button" id="classification-create">创建建议题型</button>':''}
      ${typed.date_added&&window.Cuoti.dailyAnalysis?'<button class="btn btn-txt" type="button" id="classification-analysis">查看本题分析</button>':''}</div></form>`);
    const form=document.getElementById('classification-form');
    const viewEpoch=modalRevision;
    const isCurrent=()=>form.isConnected&&modalRevision===viewEpoch&&window.location.hash===originHash;
    const changed=()=>window.dispatchEvent(new CustomEvent('cuoti:classification-changed',{detail:{publicId}}));
    document.getElementById('classification-form').addEventListener('submit',run(async()=>{
      const endpoint=draft?`/api/v2/classification-suggestions/${suggestion.id}/resolve`:`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`;
      await api(endpoint,{method:draft?'POST':'PUT',body:JSON.stringify({
        revision:typed.revision,mistake_revision:typed.mistake_revision,action:'accept',primary_type_code:document.getElementById('classification-type').value,
        knowledge_points:split(document.getElementById('classification-knowledge').value),
        method_annotations:[...document.querySelectorAll('.classification-method-row')].map(row=>{
          const name=row.querySelector('[data-method-name]').value.trim();
          return {code:methodByName(methods,name)?.code||null,name,role:row.querySelector('[data-method-role]').value};
        }).filter(item=>item.name)
      })});
      changed();if(isCurrent())await open(publicId);
    }));
    document.querySelectorAll('[data-method-name]').forEach(input=>input.addEventListener('input',()=>{
      const definition=input.closest('.classification-method-row').querySelector('[data-method-definition]');
      window.MathJax?.typesetClear?.([definition]);
      definition.textContent=methodByName(methods,input.value)?.definition||'目录外名称请保留为“未区分 / 待归类”。';
      void renderMath(definition);
    }));
    document.getElementById('classification-unlock')?.addEventListener('click',run(async()=>{
      await api(`/api/v2/mistakes/${encodeURIComponent(publicId)}/classification`,{method:'PUT',body:JSON.stringify({revision:typed.revision,action:'unlock'})});changed();if(isCurrent())await open(publicId);
    }));
    document.getElementById('classification-reject')?.addEventListener('click',run(async()=>{
      await api(`/api/v2/classification-suggestions/${suggestion.id}/resolve`,{method:'POST',body:JSON.stringify({revision:typed.revision,action:'reject'})});changed();if(isCurrent())await open(publicId);
    }));
    document.getElementById('classification-use-suggestion')?.addEventListener('click',run(()=>open(publicId,'',true)));
    document.getElementById('classification-create')?.addEventListener('click',run(()=>editType({...proposed.proposed_type,subject:typed.subject},code=>open(publicId,code,useSuggestion))));
    document.getElementById('classification-analysis')?.addEventListener('click',run(()=>window.Cuoti.dailyAnalysis.controller.open(typed.date_added,publicId)));
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
    modal('分类管理',`<div class="tabs" role="tablist" aria-label="分类管理">${[['types','题型目录'],['methods','方法目录'],['pending','待确认'],['backfill','历史补标']].map(([key,label])=>`<button type="button" class="tab${tab===key?' on':''}" data-class-tab="${key}" role="tab" aria-selected="${tab===key}">${label}</button>`).join('')}</div><div id="classification-manager"></div>`);
    document.querySelectorAll('[data-class-tab]').forEach(b=>b.addEventListener('click',run(()=>manager(b.dataset.classTab))));
    const root=document.getElementById('classification-manager');
    if(tab==='types'){
      const {items}=await api('/api/v2/question-types?all=1');
      if(!root.isConnected)return;
      root.innerHTML=`<button type="button" class="btn btn-out btn-sm" id="type-add">${icon('plus')}新增题型</button><div class="classification-table"><table class="tb"><thead><tr><th>科目</th><th>题型</th><th>定义</th><th>状态</th><th>操作</th></tr></thead><tbody>${items.map(t=>`<tr><td>${esc(t.subject)}</td><td>${esc(t.name)}</td><td>${esc(t.definition)}</td><td>${t.active?'启用':'停用'}</td><td><button type="button" class="btn btn-out btn-xs" data-type-edit="${esc(t.code)}" title="编辑题型" aria-label="编辑题型">${icon('edit')}</button></td></tr>`).join('')}</tbody></table></div>`;
      root.querySelector('#type-add').addEventListener('click',run(()=>editType()));
      root.querySelectorAll('[data-type-edit]').forEach(b=>b.addEventListener('click',run(()=>editType(items.find(t=>t.code===b.dataset.typeEdit)))));
    }else if(tab==='methods'){
      const {items}=await api('/api/v2/solution-methods');
      if(!root.isConnected)return;
      root.innerHTML=`<p class="daily-drawer-help">同一方法的别名统一归入目录名称。目录外标签保留原文，待明确后再归类。</p><div class="classification-table"><table class="tb"><thead><tr><th>方法</th><th>科目</th><th>适用操作</th><th>别名</th></tr></thead><tbody>${items.map(item=>`<tr><td>${esc(item.name)}</td><td>${esc(item.subjects.join('、'))}</td><td>${esc(item.definition)}</td><td>${esc(item.aliases.join('、'))}</td></tr>`).join('')}</tbody></table></div>`;
    }else if(tab==='pending'){
      const [{items},{items:types},{items:methods}]=await Promise.all([api('/api/v2/classification-suggestions'),api('/api/v2/question-types'),api('/api/v2/solution-methods')]);
      if(!root.isConnected)return;
      root.innerHTML=items.length?`<label class="classification-check"><input type="checkbox" id="classification-pick-all">全选可采纳建议</label><button type="button" class="btn btn-out btn-sm" id="classification-batch">${icon('check')}接受勾选建议</button>${items.map(item=>`<article class="classification-pending"><label class="classification-check"><input type="checkbox" value="${item.id}" data-class-pick${!item.classification.suggestion.is_current||!item.classification.suggestion.payload.primary_type_code?' disabled':''}>${esc(item.subject)} ${esc(item.qnum||'未标号')}</label><div class="math">${problemPreviewHTML(item.content)}</div><p>${esc(item.classification.suggestion.reason)}</p>${suggestionDetails(item.classification.suggestion.payload,types,methods)}${inline({...item,mistake_public_id:item.public_id})}</article>`).join('')}`:'<p>没有待确认的分类。</p>';
      root.querySelector('#classification-pick-all')?.addEventListener('change',event=>{
        root.querySelectorAll('[data-class-pick]:not(:disabled)').forEach(node=>{node.checked=event.target.checked});
      });
      root.querySelector('#classification-batch')?.addEventListener('click',run(async()=>{
        const selected=[...root.querySelectorAll('[data-class-pick]:checked')];
        if(!selected.length){status('请先勾选要采纳的建议');return}
        let saved=0;const errors=[];
        for(const node of selected){
          const item=items.find(i=>String(i.id)===node.value);
          try{await api(`/api/v2/classification-suggestions/${item.id}/resolve`,{method:'POST',body:JSON.stringify({action:'accept',revision:item.classification.revision,mistake_revision:item.classification.mistake_revision})});saved++}catch(error){errors.push(error.message)}
        }
        await manager('pending');status(`已接受 ${saved} 项${errors.length?'；'+errors.join('；'):''}`);
        window.dispatchEvent(new Event('cuoti:classification-changed'));
      }));
      void renderMath(root);
    }else{
      const subjects=await api('/api/subjects');
      if(!root.isConnected)return;
      root.innerHTML=`<form id="backfill-form" class="classification-form"><label>科目<select id="backfill-subject"><option value="">全部科目</option>${subjects.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></label><label>录入日期起<input type="date" id="backfill-from"></label><label>录入日期止<input type="date" id="backfill-to"></label><label class="classification-check"><input type="checkbox" id="backfill-refresh-auto">同时更新已有自动分类及方法（保留人工锁定项）</label><button class="btn btn-out" type="submit">预览补标范围</button></form><div id="backfill-preview"></div>`;
      root.querySelector('form').addEventListener('submit',run(async()=>{
        const scope={subject_id:root.querySelector('#backfill-subject').value,date_from:root.querySelector('#backfill-from').value,date_to:root.querySelector('#backfill-to').value,refresh_auto:root.querySelector('#backfill-refresh-auto').checked};
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
