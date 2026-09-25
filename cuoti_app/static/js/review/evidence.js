(function initReviewEvidence(){
  'use strict';
  const memory=new Map();
  function value(key){
    try{return sessionStorage.getItem('cuoti-evidence:'+key)||memory.get(key)||'none'}catch{return memory.get(key)||'none'}
  }
  function set(key,next){
    memory.set(key,next);
    try{sessionStorage.setItem('cuoti-evidence:'+key,next)}catch{/* The current page still keeps the selection. */}
    document.querySelectorAll('[data-evidence-key]').forEach(node=>{if(node.dataset.evidenceKey===key)node.checked=next==='hint'});
    document.querySelectorAll('[data-evidence-pass]').forEach(node=>{if(node.dataset.evidencePass===key){node.title=next==='hint'?'提示后答对':'独立答对';node.setAttribute('aria-label',node.title);const text=node.querySelector('.evidence-pass-label');if(text)text.textContent=node.title}});
    document.querySelectorAll('[data-evidence-progress]').forEach(node=>{if(node.dataset.evidenceProgress===key)node.textContent=next==='hint'?'提示后完成不升级 · 明天再测':node.dataset.independentProgress});
  }
  function control(key){return `<label class="classification-check review-assistance"><input type="checkbox" data-evidence-key="${escapeHTML(key)}"${value(key)==='hint'?' checked':''}>借助提示</label>`;}
  function bind(root){
    if(!root||root.dataset.evidenceBound)return;
    root.dataset.evidenceBound='true';
    root.addEventListener('toggle',event=>{const node=event.target;if(node.open&&node.dataset.evidenceHints)set(node.dataset.evidenceHints,'hint')},true);
    root.addEventListener('change',event=>{const node=event.target;if(node.dataset.evidenceKey)set(node.dataset.evidenceKey,node.checked?'hint':'none')});
  }
  function initialize(key,next){try{if(!memory.has(key)&&sessionStorage.getItem('cuoti-evidence:'+key)===null)set(key,next)}catch{if(!memory.has(key))set(key,next)}}
  window.Cuoti.reviewEvidence=Object.freeze({value,set,control,bind,initialize});
})();
