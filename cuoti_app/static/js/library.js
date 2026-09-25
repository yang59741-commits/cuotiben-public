const LATEX_INSERTIONS=Object.freeze({
  bold:['\\textbf{','}'],
  italic:['\\textit{','}'],
  'display-math':['$$\n','\n$$'],
  'inline-math':['$','$'],
  proof:['\\begin{proof}\n','\n\\end{proof}'],
  enumerate:['\\begin{enumerate}\n  \\item ','\n\\end{enumerate}'],
  aligned:['\\begin{aligned}\n','\n\\end{aligned}']
});

function insertLatex(before, after){
  const ta=document.getElementById('i-content');
  window.Cuoti.ui.editor.insertAtSelection(ta,{before,after});
}

function initLatexToolbar(){
  document.querySelectorAll('[data-latex-command]').forEach(button=>{
    const insertion=LATEX_INSERTIONS[button.dataset.latexCommand];
    if(!insertion)return;
    button.addEventListener('click',()=>insertLatex(insertion[0],insertion[1]));
  });
}

function validateProblemFile(file){
  if(!file)return '请选择文件。';
  if(file.size>20*1024*1024)return '文件不能超过 20 MB。';
  const ext=file.name.split('.').pop().toLowerCase();
  if(!['pdf','docx','tex','txt'].includes(ext)){
    return ext==='doc'
      ?'旧版 .doc 文件不受支持，请先另存为 .docx。'
      :'仅支持 PDF、DOCX、TeX 和 TXT 文件。';
  }
  return '';
}

function readTextProblemFile(file,textarea,status){
  const reader=new FileReader();
  reader.onload=event=>{
    textarea.value=String(event.target.result??'');
    status.textContent='已加载: '+file.name;
  };
  reader.onerror=()=>{
    showModal('错误','文件读取失败。','error');
    status.textContent='';
  };
  reader.readAsText(file);
}

async function loadProblemFile(file){
  const st=document.getElementById('i-status');
  const ta=document.getElementById('i-content');
  const validationError=validateProblemFile(file);
  if(validationError){
    showModal('无法处理文件',validationError,'error');
    return;
  }
  const ext=file.name.split('.').pop().toLowerCase();
  if(['tex','txt'].includes(ext)){
    readTextProblemFile(file,ta,st);
    return;
  }
  st.textContent=ext==='pdf'?'正在提交 DeepSeek 视觉识别任务...':'文件处理中...';
  const fd=new FormData();fd.append('file',file);
  try{
    const data=await runJobRequest('ocr',fd);
    if(data?.error)throw new Error(data.error);
    ta.value=String(data.text??'');
    const methods={deepseek_vision:'DeepSeek 视觉',docx:'本地 DOCX',direct:'本地读取'};
    st.textContent='已识别 ('+(methods[data.method]||String(data.method??'未知方式'))+'): '+file.name;
  }catch(error){
    showModal('错误','文件处理失败: '+error.message,'error');
    st.textContent='';
  }
}

function handleFileInput(e){
  const file=e.target.files[0];
  if(file)void loadProblemFile(file);
}

function initDropZone(){
  const dz=document.getElementById('drop-zone');if(!dz)return;
  if(dz.dataset.initialized==='true')return;
  dz.dataset.initialized='true';
  dz.addEventListener('click',()=>document.getElementById('i-file').click());
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag-over')});
  dz.addEventListener('dragleave',()=>dz.classList.remove('drag-over'));
  dz.addEventListener('drop',e=>{
    e.preventDefault();dz.classList.remove('drag-over');
    const file=e.dataTransfer.files[0];
    if(file)void loadProblemFile(file);
  });
}

async function submitProblem(){
  const text=document.getElementById('i-content').value.trim();
  if(!text){showModal('提示','请输入题目内容','info');return}
  const st=document.getElementById('i-status');st.textContent='AI自动识别并保存中...';
  const btn=document.querySelector('#p-input .btn-p');btn.disabled=true;btn.textContent='处理中...';
  try{
    const payload={content:text};
    const r=await runJobRequest('classification',payload);
    if(r.error){showModal('错误',r.error,'error');st.textContent='';btn.disabled=false;btn.textContent='保存到试题库';return}
    showToast('共识别并保存 '+r.count+' 道题','success');
    st.textContent='已保存 '+r.count+' 道题';
    document.getElementById('i-content').value='';
    window.clearRemoteDraftPanel?.('p-input');
  }catch(e){showModal('错误','保存失败: '+e.message,'error');st.textContent='';}
  btn.disabled=false;btn.textContent='保存到试题库';
}

initLatexToolbar();

const inputFeature=Object.freeze({
  mount:()=>{loadSubjects('i');initDropZone();return window.Cuoti.daily.mountManager()},
  unmount:()=>window.Cuoti.daily.unmountManager(),
  refresh:()=>{loadSubjects('i');return window.Cuoti.daily.refreshManager()},
  pickFile:()=>document.getElementById('i-file')?.click(),
  submit:submitProblem
});
window.Cuoti.features.register('input',inputFeature);
