function exportData(){
  showModal('确认导出','将导出全部错题、复习历史和附件为可移植 ZIP。','info',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>' +
    '<button class="btn btn-s btn-sm" id="export-confirm">确认导出</button>');
  document.getElementById('export-confirm').onclick=async function(){
    hideModal();
    try{
      const response=await fetch('/api/v3/export');
      if(!response.ok){
        let message=`导出失败（HTTP ${response.status}）`;
        try{const payload=await response.json();message=payload?.error||message}catch(_error){}
        throw new Error(message);
      }
      const b=await response.blob();
      const a=document.createElement('a');
      const objectURL=URL.createObjectURL(b);
      a.href=objectURL;
      const disposition=response.headers.get('content-disposition')||'';
      const matched=disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
      const today=window.localDateInputValue?window.localDateInputValue():new Date().toISOString().slice(0,10);
      a.download=matched?decodeURIComponent(matched[1].replace(/^"|"$/g,'')):'错题可移植备份_'+today+'.zip';
      a.click();
      URL.revokeObjectURL(objectURL);
    }catch(error){
      showModal('导出失败',error.message,'error');
    }
  };
}

function importData(e){
  const input=e.target;
  const file=input.files[0];
  if(!file)return;
  if(file.size>100*1024*1024){
    showModal('无法导入','备份文件不能超过 100 MB。','error');
    input.value='';
    return;
  }
  if(!/\.(json|zip)$/i.test(file.name)){
    showModal('无法导入','请选择 JSON 或 ZIP 备份文件。','error');
    input.value='';
    return;
  }

  const fileBody=()=>file.arrayBuffer();
  const contentType=/\.zip$/i.test(file.name)?'application/zip':'application/json';
  const previewImport=async mode=>{
    hideModal();
    try{
      const preview=await api('/api/v3/import/preview?'+new URLSearchParams({mode}),{
        method:'POST',headers:{'Content-Type':contentType},body:await fileBody()
      });
      const counts=preview?.plan?.counts||{};
      const resolutions={};
      const summary=`将新增 ${safeInteger(counts.add)} 道、合并 ${safeInteger(counts.merge)} 道；`+
        `字段冲突 ${safeInteger(counts.conflict)} 道、歧义 ${safeInteger(counts.ambiguous)} 道。`+
        (preview?.legacy?' 旧格式无事件 UUID，重复复习事件不会被静默去重。':'');
      const commit=async()=>{
        hideModal();
        try{
          const staged=await api('/api/v3/import/stage?'+new URLSearchParams({mode}),{
            method:'POST',
            headers:{'Content-Type':contentType,'X-Import-Preview-Token':preview.preview_token},
            body:await fileBody()
          });
          const result=await runJobRequest('data-import',{
            staged_file:staged.staged_file,
            input_sha256:staged.input_sha256,
            preview_token:preview.preview_token,
            resolutions,
            content_type:contentType,
            mode
          });
          input.value='';
          const message=mode==='replace'
            ?`替换完成：写入 ${safeInteger(result?.inserted)} 道。自动恢复点：${result?.recovery_backup||'已创建'}`
            :`合并完成：新增 ${safeInteger(result?.inserted)} 道，合并 ${safeInteger(result?.merged)} 道。`;
          showModal('导入成功',message,'success');nav('list');
        }catch(error){
          showModal('提交失败',error.message,'error','<button class="btn btn-out btn-sm" onclick="hideModal()">关闭</button><button class="btn btn-p btn-sm" id="import-repreview">重新预览</button>');
          document.getElementById('import-repreview').onclick=()=>previewImport(mode);
        }
      };
      if(safeInteger(counts.ambiguous)>0||safeInteger(counts.duplicate_input)>0){
        showModal('导入存在歧义',summary+' 请先修正备份中的重复或歧义身份。','error');return;
      }
      showModal(mode==='replace'?'确认替换预览':'确认合并预览',summary,mode==='replace'?'error':'info',
        '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
        `<button class="btn ${mode==='replace'?'btn-d':'btn-s'} btn-sm" id="import-commit">确认提交</button>`);
      const commitButton=document.getElementById('import-commit');
      const conflictItems=(preview?.plan?.items||[]).filter(item=>(item.conflict_fields||[]).length);
      if(mode==='merge'&&conflictItems.length){
        const body=document.getElementById('modal-body');
        const section=document.createElement('div');section.className='import-conflict-resolutions';
        const title=document.createElement('strong');title.textContent='字段冲突（默认保留本地；勾选后采用导入值）';
        section.append(title);
        const fieldLabels={qnum:'题号',content:'正文',note:'备注',answer:'答案',solution:'解析',source_kind:'来源类型',source_ref:'来源',difficulty:'难度',date_added:'录入日期'};
        conflictItems.forEach(item=>{
          const group=document.createElement('div');
          const identity=document.createElement('span');
          identity.textContent=`第 ${safeInteger(item.position)} 项：`;
          group.append(identity);
          item.conflict_fields.forEach(field=>{
            const label=document.createElement('label');
            const checkbox=document.createElement('input');checkbox.type='checkbox';
            checkbox.addEventListener('change',()=>{
              const chosen=new Set(resolutions[String(item.position)]||[]);
              if(checkbox.checked)chosen.add(field);else chosen.delete(field);
              if(chosen.size)resolutions[String(item.position)]=[...chosen];
              else delete resolutions[String(item.position)];
            });
            label.append(checkbox,document.createTextNode(fieldLabels[field]||field));
            group.append(label);
          });
          section.append(group);
        });
        body.append(document.createElement('br'),section);
      }
      if(mode==='replace'){
        commitButton.disabled=true;
        const body=document.getElementById('modal-body');
        const label=document.createElement('label');label.textContent=`请输入“替换 ${safeInteger(preview.plan.current_mistakes)} 道”确认：`;
        const confirmation=document.createElement('input');confirmation.type='text';confirmation.autocomplete='off';
        confirmation.addEventListener('input',()=>{commitButton.disabled=confirmation.value.trim()!==`替换 ${safeInteger(preview.plan.current_mistakes)} 道`});
        body.append(document.createElement('br'),label,confirmation);
      }
      commitButton.onclick=commit;
    }catch(error){
      showModal('预览失败',error.message,'error',
        '<button class="btn btn-out btn-sm" onclick="hideModal()">关闭</button>'+
        '<button class="btn btn-p btn-sm" id="import-retry">重试预览</button>');
      document.getElementById('import-retry').onclick=()=>previewImport(mode);
    }
  };
  const confirmReplace=()=>{
    showModal(
      '确认替换错题',
      `将用 ${file.name} 替换当前全部错题及其复习历史；提交前会自动创建数据库与附件恢复点，科目、章节、试卷和设置不会改变。`,
      'error',
      '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
      '<button class="btn btn-d btn-sm" id="import-replace-confirm">确认替换</button>'
    );
    document.getElementById('import-replace-confirm').onclick=()=>previewImport('replace');
  };

  showModal(
    '选择导入方式',
    `已选择 ${file.name}。两种方式都会先生成可确认预览；替换提交前还会自动创建恢复点。`,
    'info',
    '<button class="btn btn-out btn-sm" onclick="hideModal()">取消</button>'+
    '<button class="btn btn-s btn-sm" id="import-merge">安全合并</button>'+
    '<button class="btn btn-d btn-sm" id="import-replace">替换错题</button>'
  );
  document.getElementById('import-merge').onclick=()=>previewImport('merge');
  document.getElementById('import-replace').onclick=confirmReplace;
}

const dataFeature=Object.freeze({
  mount:()=>{},
  unmount:()=>{},
  refresh:()=>{},
  exportData,
  pickImport:()=>document.getElementById('imp')?.click(),
  importData
});
window.Cuoti.data=dataFeature;
window.Cuoti.features.register('data',dataFeature);
