(function initEditorUtilities(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.ui=window.Cuoti.ui||{};

  // preventScroll keeps a side panel (such as the LaTeX dock) in place while
  // focus returns to the textarea after an insertion.
  function insertAtSelection(textarea,{before='',after='',placeholder='',useSelection=true,selectContent=true,preventScroll=false}={}){
    if(!(textarea instanceof HTMLTextAreaElement))throw new TypeError('需要文本输入框');
    const start=textarea.selectionStart;
    const end=textarea.selectionEnd;
    const selected=textarea.value.slice(start,end);
    const content=(useSelection&&selected)||String(placeholder||'');
    const inserted=String(before)+content+String(after);
    textarea.setRangeText(inserted,start,end,'end');
    const contentStart=start+String(before).length;
    if(preventScroll)textarea.focus({preventScroll:true});else textarea.focus();
    textarea.selectionStart=selectContent?contentStart:start+inserted.length;
    textarea.selectionEnd=selectContent?contentStart+content.length:start+inserted.length;
    textarea.dispatchEvent(new Event('input',{bubbles:true}));
    return {start:contentStart,end:contentStart+content.length,text:inserted};
  }

  async function copyText(value){
    const text=String(value||'');
    if(navigator.clipboard?.writeText){
      await navigator.clipboard.writeText(text);
      return;
    }
    const fallback=document.createElement('textarea');
    fallback.value=text;fallback.readOnly=true;fallback.hidden=false;
    fallback.style.position='fixed';fallback.style.opacity='0';
    document.body.appendChild(fallback);fallback.select();
    const copied=document.execCommand('copy');fallback.remove();
    if(!copied)throw new Error('浏览器未允许复制');
  }

  /* Live formula preview: a collapsible pane under a textarea, rendered with
     the local MathJax after typing pauses.  Its open state is remembered. */
  const PREVIEW_DELAY=400;
  const previewTimers=new Map();

  function previewFor(id){
    return document.querySelector(`.math-live-preview[data-preview-for="${CSS.escape(String(id||''))}"]`);
  }

  function previewStorageKey(id){
    return 'cuoti.mathPreview.'+id;
  }

  async function refreshPreview(id){
    const pane=previewFor(id);
    const source=document.getElementById(id);
    if(!pane||!source)return;
    clearTimeout(previewTimers.get(id));previewTimers.delete(id);
    if(!pane.open)return;
    const body=pane.querySelector('.math-live-preview-body');
    if(!body)return;
    const value=String(source.value||'').trim();
    window.MathJax?.typesetClear?.([body]);
    if(!value){
      body.innerHTML='<span class="math-live-preview-empty">输入内容后在这里预览公式效果</span>';
      return;
    }
    body.innerHTML=pane.dataset.previewMode==='problem'
      ?window.Cuoti.ui.render.problemPreview(value,'math-live-preview-content')
      :`<div class="math math-live-preview-content math-live-preview-plain">${window.Cuoti.ui.render.escape(value)}</div>`;
    const target=body.querySelector('.math');
    if(target&&typeof renderMathNodes==='function')await renderMathNodes(target,{force:true});
  }

  function schedulePreview(id){
    if(!previewFor(id)?.open)return;
    clearTimeout(previewTimers.get(id));
    previewTimers.set(id,setTimeout(()=>void refreshPreview(id),PREVIEW_DELAY));
  }

  /* The summary bear wakes up when live preview opens and dozes off when it
     closes; a remembered open state is restored already awake. */
  const BEAR_MOTIONS=Object.freeze({wake:'previewBearWake',doze:'previewBearDoze'});

  function animatePreviewBear(pane){
    const bear=pane.querySelector?.('.math-live-preview-bear');
    if(bear)bear.dataset.motion=pane.open?'wake':'doze';
  }

  function bindPreviews(){
    if(typeof document.querySelectorAll!=='function'||typeof document.addEventListener!=='function')return;
    document.querySelectorAll('.math-live-preview[data-preview-for]').forEach(pane=>{
      try{
        if(!pane.open&&localStorage.getItem(previewStorageKey(pane.dataset.previewFor))==='1'){
          pane.dataset.previewRestoring='1';
          pane.open=true;
        }
      }catch{/* storage may be unavailable */}
      if(pane.open)void refreshPreview(pane.dataset.previewFor);
    });
    // Capture phase also sees the non-bubbling input events fired by counters.
    document.addEventListener('input',event=>{
      const id=event.target?.id;
      if(id&&previewFor(id))schedulePreview(id);
    },true);
    document.addEventListener('toggle',event=>{
      const pane=event.target;
      if(!pane?.matches?.('.math-live-preview[data-preview-for]'))return;
      try{localStorage.setItem(previewStorageKey(pane.dataset.previewFor),pane.open?'1':'0')}catch{/* ignore */}
      if(pane.dataset.previewRestoring)delete pane.dataset.previewRestoring;
      else animatePreviewBear(pane);
      if(pane.open)void refreshPreview(pane.dataset.previewFor);
    },true);
    document.addEventListener('animationend',event=>{
      const bear=event.target;
      if(bear?.matches?.('.math-live-preview-bear')&&event.animationName===BEAR_MOTIONS[bear.dataset.motion]){
        delete bear.dataset.motion;
      }
    },true);
  }

  if(document.readyState==='loading'&&typeof document.addEventListener==='function'){
    document.addEventListener('DOMContentLoaded',bindPreviews,{once:true});
  }else bindPreviews();

  window.Cuoti.ui.editor=Object.freeze({insertAtSelection,copyText,refreshPreview});
})();
