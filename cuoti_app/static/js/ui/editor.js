(function initEditorUtilities(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.ui=window.Cuoti.ui||{};

  function insertAtSelection(textarea,{before='',after='',placeholder='',useSelection=true,selectContent=true}={}){
    if(!(textarea instanceof HTMLTextAreaElement))throw new TypeError('需要文本输入框');
    const start=textarea.selectionStart;
    const end=textarea.selectionEnd;
    const selected=textarea.value.slice(start,end);
    const content=(useSelection&&selected)||String(placeholder||'');
    const inserted=String(before)+content+String(after);
    textarea.setRangeText(inserted,start,end,'end');
    const contentStart=start+String(before).length;
    textarea.focus();
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

  window.Cuoti.ui.editor=Object.freeze({insertAtSelection,copyText});
})();
