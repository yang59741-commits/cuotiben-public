(function initExamPreview(){
'use strict';
const examPage=window.Cuoti.exam;

function renderMaterialUnderstanding(payload){
  const root=document.getElementById('exam-material-understanding');if(!root)return;
  if(!payload){root.hidden=true;return}
  const total=Math.max(0,safeInteger(payload.total_count));
  const analyzed=Math.max(0,safeInteger(payload.analyzed_count));
  const years=Array.isArray(payload.years)?payload.years.map(value=>safeInteger(value)).filter(Boolean):[];
  root.hidden=false;
  root.open=!payload.complete;
  root.querySelector('summary').textContent=`材料理解状态 · 已理解 ${analyzed}/${total} 道有效题`;
  root.querySelector('div').textContent=`覆盖 ${safeInteger(payload.chapter_count)} 个章节、${safeInteger(payload.source_count)} 个来源${years.length?'、'+years.join('、')+' 年':''}；本次新增 ${safeInteger(payload.batch_count)} 批，复用 ${safeInteger(payload.reused_count)} 道缓存。`;
}

let examPreviewRevision=0;

let examSelectionExplanationRevision=0;

let examSelectionExplanationController=null;

function resetExamSelectionExplanation(){
  if(examSelectionExplanationController)examSelectionExplanationController.abort();
  examSelectionExplanationController=null;
  examSelectionExplanationRevision+=1;
  document.getElementById('exam-selection-explanation')?.remove();
}

function selectionExplanationRetry(body,retry){
  const button=document.createElement('button');
  button.type='button';button.className='btn btn-out btn-sm';
  button.innerHTML=icon('refresh')+'重试';
  button.addEventListener('click',retry);
  body.append(button);
}

function renderSelectionExplanation(payload,body,retry){
  const status=String(payload?.status||'');
  if(!['available','not_applicable','unavailable'].includes(status)){
    throw new Error('选题依据响应格式无效');
  }
  const summary=String(payload?.summary_text||'').trim();
  const scope=String(payload?.scope_text||'').trim();
  body.replaceChildren();
  const lead=document.createElement('div');lead.className='exam-selection-explanation-lead';
  lead.innerHTML=`<strong>${escapeHTML(summary||(
    status==='not_applicable'?'本试卷不适用选题依据':'选题依据暂不可用'
  ))}</strong>${scope?`<span>${escapeHTML(scope)}</span>`:''}`;
  body.append(lead);
  if(status!=='available'){
    body.classList.toggle('is-not-applicable',status==='not_applicable');
    if(status==='unavailable')selectionExplanationRetry(body,retry);
    return;
  }
  body.classList.remove('is-not-applicable');
  if(!Array.isArray(payload.items))throw new Error('选题依据题目列表无效');
  const list=document.createElement('div');list.className='exam-selection-explanation-items';
  payload.items.forEach(raw=>{
    const position=safeInteger(raw?.position);
    const reason=String(raw?.reason_text||'').trim();
    const history=String(raw?.history_text||'').trim();
    const comparison=String(raw?.comparison_text||'').trim();
    if(position<1||!reason||!history)throw new Error('选题依据题目内容无效');
    const item=document.createElement('section');item.className='exam-selection-explanation-item';
    item.innerHTML=`
      <h4>第 ${position} 题</h4>
      <p><span>入选原因</span>${escapeHTML(reason)}</p>
      <p><span>历史情况</span>${escapeHTML(history)}</p>
      ${comparison?`<p><span>候选比较</span>${escapeHTML(comparison)}</p>`:''}`;
    const alternative=raw?.alternative;
    if(alternative!==null&&alternative!==undefined){
      const label=String(alternative?.label||'').trim();
      const content=String(alternative?.content||'');
      if(!label||!content.trim())throw new Error('替代题比较内容无效');
      const details=document.createElement('details');
      details.className='exam-selection-alternative';
      details.innerHTML=`<summary>${icon('chevronDown')}<span>${escapeHTML(label)}</span></summary>`;
      let rendered=false;
      details.addEventListener('toggle',()=>{
        if(!details.open||rendered)return;
        rendered=true;
        const alternativeBody=document.createElement('div');
        alternativeBody.className='exam-selection-alternative-content math';
        alternativeBody.innerHTML=latexToHtml(content);
        details.append(alternativeBody);
        void renderMath(alternativeBody);
      });
      item.append(details);
    }
    list.append(item);
  });
  body.append(list);
}

function mountExamSelectionExplanation(examId,result){
  const details=document.createElement('details');
  details.id='exam-selection-explanation';
  details.className='exam-selection-explanation';
  details.innerHTML=`
    <summary>${icon('listChecks')}<span>选题依据</span>${icon('chevronDown','exam-selection-explanation-chevron')}</summary>
    <div class="exam-selection-explanation-body" aria-live="polite"></div>`;
  const content=document.getElementById('exam-content');
  if(content?.parentElement===result)content.insertAdjacentElement('afterend',details);
  else result.append(details);
  const body=details.querySelector('.exam-selection-explanation-body');
  let state='idle';
  const load=async()=>{
    if(state==='loading'||examPage.state.currentExamPreviewId!==examId)return;
    state='loading';
    const revision=++examSelectionExplanationRevision;
    const navigationToken=window.navigationRevision;
    if(examSelectionExplanationController)examSelectionExplanationController.abort();
    examSelectionExplanationController=new AbortController();
    body.classList.remove('is-not-applicable');
    body.innerHTML=`<div class="exam-selection-explanation-loading">${icon('loader','icon-spin')}<span>正在读取选题依据...</span></div>`;
    try{
      const payload=await examPage.api.request(
        `/api/v2/exams/${examId}/selection-explanation`,
        {signal:examSelectionExplanationController.signal}
      );
      if(
        revision!==examSelectionExplanationRevision||
        examPage.state.currentExamPreviewId!==examId||
        navigationToken!==window.navigationRevision
      )return;
      renderSelectionExplanation(payload,body,()=>{state='idle';void load()});
      state='loaded';
    }catch(error){
    if(error?.name==='AbortError')return;
      if(error?.name==='AbortError'||revision!==examSelectionExplanationRevision)return;
      if(examPage.state.currentExamPreviewId!==examId||navigationToken!==window.navigationRevision)return;
      state='failed';body.replaceChildren();
      const message=document.createElement('div');
      message.className='exam-selection-explanation-error';
      message.textContent='选题依据读取失败：'+error.message;
      body.append(message);
      selectionExplanationRetry(body,()=>{state='idle';void load()});
    }
  };
  details.addEventListener('toggle',()=>{
    if(details.open&&state==='idle')void load();
  });
}

function hideExamPreview(){
  examPreviewRevision+=1;
  examPage.state.currentExamPreviewId=null;
  resetExamSelectionExplanation();
  const result=document.getElementById('exam-result');
  const content=document.getElementById('exam-content');
  if(result)result.style.display='none';
  if(content)content.style.display='none';
}

async function viewExam(id){
  resetExamSelectionExplanation();
  examPage.state.currentExamPreviewId=null;
  const previewRevision=++examPreviewRevision;
  const navigationToken=window.navigationRevision;
  let data;
  try{
    data=await examPage.api.request('/api/v2/exams/'+id);
  }catch(error){
    if(error?.name==='AbortError')return;
    if(previewRevision===examPreviewRevision&&navigationToken===window.navigationRevision){
      showModal('试卷读取失败',error.message,'error');
    }
    return;
  }
  if(previewRevision!==examPreviewRevision||navigationToken!==window.navigationRevision)return;
  if(data.error){showModal('提示', '该试卷不存在或已被删除', 'error');return}
  const result=document.getElementById('exam-result');
  const content=document.getElementById('exam-content');
  const empty=document.getElementById('exam-empty');
  result.style.display='';
  content.classList.add('exam-preview-typesetting');
  content.innerHTML=reviewMarkerBannerHTML(data.items)+latexToHtml(data.content);
  const trainingLabels={general:'综合巩固',concept:'概念辨析',method_selection:'方法选择',condition_check:'条件核对',transfer:'迁移应用'};
  const trainingTarget=String(data.summary?.training_target||'');
  if(trainingTarget){
    const counts=data.summary?.training_target_counts||{};
    const state=safeInteger(counts.mismatch)?'目标复核未通过':safeInteger(counts.unknown)?'目标复核状态未知':'目标复核通过';
    content.insertAdjacentHTML('afterbegin',`<div class="exam-review-markers"><div class="exam-review-markers-title"><strong>训练目标：${escapeHTML(trainingLabels[trainingTarget]||trainingTarget)}</strong><span>${escapeHTML(state)}</span></div></div>`);
  }
  content.style.display='block';
  empty.textContent='正在渲染试卷预览...';
  empty.style.display='block';
  const rendered=await renderMath(content);
  if(previewRevision!==examPreviewRevision||navigationToken!==window.navigationRevision)return;
  const leaked=hasVisibleLatexLeak(content);
  content.classList.remove('exam-preview-typesetting');
  empty.style.display='none';
  if(!rendered||leaked){
    content.innerHTML='<div class="exam-preview-error">公式预览渲染失败，请稍后重新打开；PDF 文件不受影响。</div>';
  }
  examPage.state.currentExamPreviewId=id;
  mountExamSelectionExplanation(id,result);
  if(data.pdf_url){
    const pdfURL=safeLocalURL(data.pdf_url);
    let a=document.getElementById('pdf-link');
    if(!a){a=document.createElement('a');a.id='pdf-link';a.className='btn btn-s';a.target='_blank';a.innerHTML=icon('download')+'下载PDF';document.getElementById('exam-result').appendChild(a)}
    a.rel='noopener';
    if(pdfURL){
      a.href=pdfURL;a.style.display='';
    }else{
      a.removeAttribute('href');a.style.display='none';
    }
  }else{
    const oldLink=document.getElementById('pdf-link');
    if(oldLink){
      oldLink.removeAttribute('href');
      oldLink.style.display='none';
    }
  }
  let recompileButton=document.getElementById('exam-recompile');
  if(data.recompile_available){
    if(!recompileButton){
      recompileButton=document.createElement('button');
      recompileButton.id='exam-recompile';recompileButton.className='btn btn-out';
      recompileButton.textContent='重新编译';
      document.getElementById('exam-result').appendChild(recompileButton);
    }
    recompileButton.hidden=false;
    recompileButton.onclick=()=>examPage.controller.recompileExam(id);
  }else if(recompileButton){
    recompileButton.hidden=true;
  }
  let attemptButton=document.getElementById('exam-start-attempt');
  if(!attemptButton){
    attemptButton=document.createElement('button');attemptButton.id='exam-start-attempt';
    attemptButton.className='btn btn-s';attemptButton.textContent='开始作答';
    document.getElementById('exam-result').appendChild(attemptButton);
  }
  attemptButton.onclick=()=>examPage.attempts.startExamAttempt(id);
  let historyButton=document.getElementById('exam-attempt-history');
  if(!historyButton){
    historyButton=document.createElement('button');historyButton.id='exam-attempt-history';
    historyButton.className='btn btn-out';historyButton.textContent='作答记录';
    document.getElementById('exam-result').appendChild(historyButton);
  }
  historyButton.onclick=()=>examPage.attempts.showExamAttempts(id);
}

function hasVisibleLatexLeak(root){
  if(!root)return true;
  if(root.querySelector('mjx-merror,.MathJax_Error'))return true;
  const renderedText=String(root.textContent||'');
  return /\\[A-Za-z@]+|\\[\x28\x29\x5b\x5d]/.test(renderedText);
}

function shieldLatexMath(html){
  const mathSegments=[];
  html=html.replace(
    /\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$\$[\s\S]*?\$\$|\$(?!\$)(?:\\.|[^$\\])+\$/g,
    segment=>{
      const token=`\uE000${mathSegments.length}\uE001`;
      mathSegments.push(segment);
      return token;
    }
  );
  return {html,mathSegments};
}

function stripLatexPreamble(html){
  html=html.replace(/\\documentclass(?:\[[^\]]*\])?\{[^}]*\}/g,'');
  html=html.replace(/\\usepackage(?:\[[^\]]*\])?\{[^}]*\}/g,'');
  html=html.replace(/\\geometry\{[^}]*\}/g,'');
  html=html.replace(/\\linespread\{[^}]*\}/g,'');
  html=html.replace(/\\definecolor\{[^}]*\}\{[^}]*\}\{[^}]*\}/g,'');
  html=html.replace(/\\newcounter\{[^}]*\}/g,'');
  html=html.replace(/\\newenvironment\{[^}]*\}\{[^}]*\}\{[^}]*\}/g,'');
  html=html.replace(/\\(?:begin|end)\{document\}/g,'');
  html=html.replace(/\\(?:maketitle|tableofcontents)\b/g,'');
  html=html.replace(/\\(?:title|author|date)\{[^}]*\}/g,'');
  return html;
}

function restoreLatexMath(html,mathSegments){
  return html.replace(/\uE000(\d+)\uE001/g,(_match,index)=>mathSegments[Number(index)]||'');
}

function latexToHtml(html){
  html=escapeHTML(String(html??'').replace(/\r\n?/g,'\n'));
  // MathJax does not know the short vmat alias sometimes emitted by AI.
  html=html.replace(/\\(begin|end)\{vmat\}/g,'\\$1{vmatrix}');
  const shielded=shieldLatexMath(html);
  html=stripLatexPreamble(shielded.html);
  // section* -> h2
  html=html.replace(/\\section\*\{([^}]*)\}/g,'<h2 style="font-size:18px;font-weight:700;margin:16px 0 10px;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:6px">$1</h2>');
  // subsection -> question heading
  html=html.replace(/\\subsection\*?\{([^}]*)\}/g,'<h3 class="exam-question-title">$1</h3>');
  // begin/end problem -> numbered card, including optional [title][badge] labels
  let problemNumber=0;
  html=html.replace(/\\begin\{problem\}((?:\s*\[[^\]\r\n]*\])*)/g,(_match,options)=>{
    problemNumber+=1;
    const labels=[...String(options||'').matchAll(/\[([^\]]*)\]/g)]
      .map(match=>match[1].trim())
      .filter(Boolean);
    const title=labels.shift()||'';
    const badges=labels.map(label=>`<span class="exam-problem-badge">${label}</span>`).join('');
    return `<section class="exam-problem"><div class="exam-problem-heading">${icon('fileText','inline-icon')}<span class="exam-problem-index">题目 ${problemNumber}</span>${title?`<span class="exam-problem-title">${title}</span>`:''}${badges}</div><div class="exam-problem-body">`;
  });
  html=html.replace(/\\end\{problem\}/g,'</div></section>');
  // begin/end note -> hint box
  html=html.replace(/\\begin\{note\}/g,`<aside class="exam-note">${icon('lightbulb','inline-icon')}<div>`);
  html=html.replace(/\\end\{note\}/g,'</div></aside>');
  // proof/solution environments are document structure, not MathJax input
  html=html.replace(/\\begin\{proof\}(?:\[[^\]]*\])?/g,'<div class="exam-proof"><strong>证明：</strong>');
  html=html.replace(/\\end\{proof\}/g,'</div>');
  html=html.replace(/\\begin\{solution\}/g,'<div class="exam-proof"><strong>解答：</strong>');
  html=html.replace(/\\end\{solution\}/g,'</div>');
  html=html.replace(/\\begin\{center\}/g,'<div class="exam-center">');
  html=html.replace(/\\end\{center\}/g,'</div>');
  // begin/end shaded -> remove (problem handles styling)
  html=html.replace(/\\begin\{shaded\}/g,'');
  html=html.replace(/\\end\{shaded\}/g,'');
  // itemize
  html=html.replace(/\\begin\{itemize\}/g,'<ul class="exam-list">');
  html=html.replace(/\\end\{itemize\}/g,'</ul>');
  // enumerate
  html=html.replace(/\\begin\{enumerate\}(?:\[[^\]]*\])?/g,'<ol class="exam-list">');
  html=html.replace(/\\end\{enumerate\}/g,'</ol>');
  // item, including explicit labels such as \item[(1)]
  html=html.replace(/\\item(?:\s*\[([^\]]*)\])?\s*/g,(_match,label)=>{
    if(label===undefined)return '<li>';
    return `<li class="exam-list-item-custom"><span class="exam-list-label">${label}</span>`;
  });
  // textbf
  html=html.replace(/\\textbf\{([^}]*)\}/g,'<strong>$1</strong>');
  // textit
  html=html.replace(/\\textit\{([^}]*)\}/g,'<em>$1</em>');
  html=html.replace(/\\emph\{([^}]*)\}/g,'<em>$1</em>');
  // par
  html=html.replace(/\\par\b/g,'<br>');
  // noindent
  html=html.replace(/\\noindent\b/g,'');
  // stepcounter (remove)
  html=html.replace(/\\stepcounter\{[^}]*\}/g,'');
  // newline markers
  html=html.replace(/\\baselineskip\b/g,'');
  html=html.replace(/\\bigskip\b/g,'<br>');
  html=html.replace(/\\medskip\b/g,'<br>');
  html=html.replace(/\\smallskip\b/g,'<br>');
  html=html.replace(/\\vspace\*?\{[^}]*\}/g,'<br>');
  html=html.replace(/\\hspace\*?\{[^}]*\}/g,'');
  html=html.replace(/\\vfill\b/g,'');
  html=html.replace(/\\hfill\b/g,'');
  html=html.replace(/\\newpage\b/g,'<hr>');
  html=html.replace(/\\clearpage\b/g,'<hr>');
  html=html.replace(/\\qed\b/g,'∎');
  // Clean up double <br> or orphaned </li>
  html=html.replace(/<br>\s*<br>/g,'<br>');
  // Fix any <li> not inside list - wrap them
  if(/<li(?:\s|>)/.test(html)&&!/<[ou]l(?:\s|>)/.test(html)){
    html='<ul class="exam-list">'+html+'</ul>';
  }
  return restoreLatexMath(html,shielded.mathSegments);
}

function reviewMarkerBannerHTML(items){
  if(!Array.isArray(items))return '';
  const notices=[];
  items.forEach(item=>{
    const position=safeInteger(item?.position);
    const metadata=item?.source_metadata&&typeof item.source_metadata==='object'?item.source_metadata:{};
    const raw=Array.isArray(metadata.review_markers)
      ?metadata.review_markers
      :(metadata.review_marker?[metadata.review_marker]:[]);
    raw.forEach(marker=>{
      if(!marker||typeof marker!=='object')return;
      const label=String(marker.label||'题面待核对').trim();
      const message=String(marker.message||'该题存在尚未解决的审阅提示。').trim();
      if(!position||!label)return;
      notices.push(`<div class="exam-review-marker"><span class="exam-review-marker-position">第${position}题</span><strong>${escapeHTML(label)}</strong><span>${escapeHTML(message)}</span></div>`);
    });
  });
  if(!notices.length)return '';
  return `<aside class="exam-review-markers" role="note"><div class="exam-review-markers-title">${icon('circleAlert')}<strong>题面审阅提示</strong><span>仅作提示，不影响本卷选题与难度。</span></div><div class="exam-review-markers-list">${notices.join('')}</div></aside>`;
}

let binding=null;
function bind(){
  if(binding)return;
  binding=new AbortController();
window.addEventListener('hashchange',()=>{
  examPage.state.currentExamPreviewId=null;
  resetExamSelectionExplanation();
},{signal:binding.signal});
}
function unbind(){binding?.abort();binding=null}

examPage.preview=Object.freeze({
  renderMaterialUnderstanding,
  resetExamSelectionExplanation,
  selectionExplanationRetry,
  renderSelectionExplanation,
  mountExamSelectionExplanation,
  hideExamPreview,
  viewExam,
  hasVisibleLatexLeak,
  shieldLatexMath,
  stripLatexPreamble,
  restoreLatexMath,
  latexToHtml,
  reviewMarkerBannerHTML,
  bind,
  unbind
});
})();
