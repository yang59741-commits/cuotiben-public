(function initCustomSelects(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.ui=window.Cuoti.ui||{};

  const states=new WeakMap();
  let openState=null;
  let generatedId=0;
  let typeaheadTimer=0;
  let typeaheadText='';

  function optionItems(select){
    return [...select.options].map((option,index)=>({
      index,
      value:String(option.value??''),
      label:String(option.label||option.textContent||''),
      disabled:Boolean(option.disabled||option.parentElement?.disabled),
      selected:index===select.selectedIndex
    }));
  }

  function fieldLabel(select){
    const explicit=select.getAttribute('aria-label');
    if(explicit)return explicit.trim();
    const label=[...select.labels||[]][0];
    if(label){
      const clone=label.cloneNode(true);
      clone.querySelectorAll('select,.select-control').forEach(element=>element.remove());
      const text=clone.textContent.replace(/\s+/g,' ').trim();
      if(text)return text;
    }
    return select.title||'选择项';
  }

  function currentLabel(select){
    const option=select.options[select.selectedIndex];
    return option?String(option.label||option.textContent||''):'请选择';
  }

  function copySurface(state){
    const style=getComputedStyle(state.wrapper);
    for(const property of [
      '--card','--surface','--border','--text','--muted','--accent','--accent-soft',
      '--accent-strong','--accent-rgb','--gray-100','--gray-200','--gray-300',
      '--focus-ring','--radius-control','--shadow-lg','--dur-fast'
    ]){
      const value=style.getPropertyValue(property);
      if(value)state.menu.style.setProperty(property,value);
    }
    state.menu.style.fontFamily=style.fontFamily;
    state.menu.style.fontSize=getComputedStyle(state.trigger).fontSize;
  }

  function setActive(state,index,{scroll=true}={}){
    const items=optionItems(state.select);
    if(!items[index]||items[index].disabled)return;
    state.activeIndex=index;
    state.menu?.querySelectorAll('.select-option').forEach(option=>{
      const active=Number(option.dataset.index)===index;
      option.classList.toggle('is-active',active);
      if(active){
        state.trigger.setAttribute('aria-activedescendant',option.id);
        if(scroll)option.scrollIntoView({block:'nearest'});
      }
    });
  }

  function enabledIndex(state,start,direction){
    const items=optionItems(state.select);
    if(!items.length)return -1;
    let index=start;
    for(let checked=0;checked<items.length;checked+=1){
      index=(index+direction+items.length)%items.length;
      if(!items[index].disabled)return index;
    }
    return -1;
  }

  function edgeIndex(state,fromEnd=false){
    const items=optionItems(state.select);
    const indexes=fromEnd?[...items.keys()].reverse():[...items.keys()];
    return indexes.find(index=>!items[index].disabled)??-1;
  }

  function positionMenu(state){
    if(!state.menu||!state.select.isConnected)return;
    const rect=state.trigger.getBoundingClientRect();
    const viewportWidth=document.documentElement.clientWidth;
    const viewportHeight=window.visualViewport?.height||window.innerHeight;
    const margin=8;
    const gap=6;
    const width=Math.max(120,Math.min(rect.width,viewportWidth-margin*2));
    const left=Math.max(margin,Math.min(rect.left,viewportWidth-width-margin));
    const below=Math.max(0,viewportHeight-rect.bottom-gap-margin);
    const above=Math.max(0,rect.top-gap-margin);
    const desired=Math.min(280,state.menu.scrollHeight||280);
    const openAbove=below<Math.min(160,desired)&&above>below;
    const available=openAbove?above:below;
    const maxHeight=Math.max(72,Math.min(280,available));
    state.menu.style.left=left+'px';
    state.menu.style.width=width+'px';
    state.menu.style.maxHeight=maxHeight+'px';
    state.menu.classList.toggle('opens-above',openAbove);
    const renderedHeight=Math.min(state.menu.scrollHeight,maxHeight);
    state.menu.style.top=(openAbove
      ?Math.max(margin,rect.top-gap-renderedHeight)
      :Math.min(viewportHeight-margin-renderedHeight,rect.bottom+gap))+'px';
  }

  function renderMenu(state){
    if(!state.menu)return;
    const items=optionItems(state.select);
    state.menu.replaceChildren();
    items.forEach(item=>{
      const option=document.createElement('div');
      option.id=state.menu.id+'-option-'+item.index;
      option.className='select-option';
      option.dataset.index=String(item.index);
      option.setAttribute('role','option');
      option.setAttribute('aria-label',item.label);
      option.setAttribute('aria-selected',String(item.selected));
      option.setAttribute('aria-disabled',String(item.disabled));
      option.classList.toggle('is-selected',item.selected);
      option.classList.toggle('is-disabled',item.disabled);
      const label=document.createElement('span');
      label.textContent=item.label;
      option.appendChild(label);
      state.menu.appendChild(option);
    });
    const selected=items.find(item=>item.selected&&!item.disabled)?.index;
    const fallback=edgeIndex(state);
    setActive(state,selected??fallback,{scroll:false});
    copySurface(state);
    positionMenu(state);
  }

  function sync(target){
    const state=target?.select?target:states.get(target);
    if(!state)return;
    const label=currentLabel(state.select);
    state.value.textContent=label;
    state.trigger.disabled=state.select.disabled;
    state.wrapper.classList.toggle('is-disabled',state.select.disabled);
    state.trigger.setAttribute('aria-disabled',String(state.select.disabled));
    state.trigger.setAttribute('aria-label',state.label+'：'+label);
    if(state.select.disabled&&openState===state)close(state,{restoreFocus:false});
    else if(openState===state)renderMenu(state);
  }

  function close(target=openState,{restoreFocus=true}={}){
    const state=target?.select?target:states.get(target);
    if(!state||openState!==state)return;
    state.menu?.remove();
    state.menu=null;
    state.activeIndex=-1;
    state.trigger.setAttribute('aria-expanded','false');
    state.trigger.removeAttribute('aria-controls');
    state.trigger.removeAttribute('aria-activedescendant');
    state.wrapper.classList.remove('is-open');
    openState=null;
    typeaheadText='';
    window.clearTimeout(typeaheadTimer);
    if(restoreFocus&&state.trigger.isConnected)state.trigger.focus();
  }

  function open(target){
    const state=target?.select?target:states.get(target);
    if(!state||state.select.disabled)return;
    if(openState===state){close(state);return}
    if(openState)close(openState,{restoreFocus:false});
    sync(state);
    const menu=document.createElement('div');
    menu.id=state.menuId;
    menu.className='select-menu';
    menu.setAttribute('role','listbox');
    menu.setAttribute('aria-label',state.label);
    state.menu=menu;
    document.body.appendChild(menu);
    openState=state;
    state.wrapper.classList.add('is-open');
    state.trigger.setAttribute('aria-expanded','true');
    state.trigger.setAttribute('aria-controls',menu.id);
    renderMenu(state);
  }

  function choose(state,index){
    const item=optionItems(state.select)[index];
    if(!item||item.disabled)return;
    const changed=state.select.selectedIndex!==index;
    state.select.selectedIndex=index;
    sync(state);
    close(state);
    if(changed){
      state.select.dispatchEvent(new Event('input',{bubbles:true}));
      state.select.dispatchEvent(new Event('change',{bubbles:true}));
      queueMicrotask(()=>sync(state));
    }
  }

  function move(state,direction){
    const start=state.activeIndex>=0?state.activeIndex:state.select.selectedIndex;
    const next=enabledIndex(state,start,direction);
    if(next>=0)setActive(state,next);
  }

  function typeahead(state,key){
    typeaheadText=(typeaheadText+key).toLocaleLowerCase('zh-CN');
    window.clearTimeout(typeaheadTimer);
    typeaheadTimer=window.setTimeout(()=>{typeaheadText=''},700);
    const items=optionItems(state.select);
    const start=state.activeIndex>=0?state.activeIndex:state.select.selectedIndex;
    for(let offset=1;offset<=items.length;offset+=1){
      const index=(start+offset)%items.length;
      if(!items[index].disabled&&items[index].label.toLocaleLowerCase('zh-CN').startsWith(typeaheadText)){
        setActive(state,index);
        return;
      }
    }
  }

  function onTriggerKeydown(event,state){
    const openNow=openState===state;
    if(['ArrowDown','ArrowUp'].includes(event.key)){
      event.preventDefault();
      if(!openNow)open(state);
      else move(state,event.key==='ArrowDown'?1:-1);
      return;
    }
    if(['Enter',' '].includes(event.key)){
      event.preventDefault();
      if(!openNow)open(state);
      else if(state.activeIndex>=0)choose(state,state.activeIndex);
      return;
    }
    if(event.key==='Escape'&&openNow){event.preventDefault();close(state);return}
    if(event.key==='Home'&&openNow){event.preventDefault();setActive(state,edgeIndex(state));return}
    if(event.key==='End'&&openNow){event.preventDefault();setActive(state,edgeIndex(state,true));return}
    if(event.key==='Tab'&&openNow){close(state,{restoreFocus:false});return}
    if(openNow&&event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
      typeahead(state,event.key);
    }
  }

  function enhance(select){
    if(!(select instanceof HTMLSelectElement)||select.multiple||select.dataset.nativeSelect!==undefined)return null;
    if(states.has(select))return states.get(select);

    const label=fieldLabel(select);
    const wrapper=document.createElement('div');
    wrapper.className='select-control';
    if(select.classList.contains('sel-sm'))wrapper.classList.add('select-control-sm');
    const trigger=document.createElement('button');
    const value=document.createElement('span');
    const chevron=document.createElement('span');
    const identity=select.id||'generated-'+(++generatedId);
    const menuId='select-menu-'+identity.replace(/[^a-zA-Z0-9_-]/g,'-');
    trigger.type='button';
    trigger.id=identity+'-select-trigger';
    trigger.className='select-trigger';
    trigger.setAttribute('role','combobox');
    trigger.setAttribute('aria-haspopup','listbox');
    trigger.setAttribute('aria-expanded','false');
    trigger.setAttribute('autocomplete','off');
    value.className='select-value';
    chevron.className='select-chevron';
    chevron.setAttribute('aria-hidden','true');
    chevron.innerHTML=typeof icon==='function'?icon('chevronDown'):'';
    trigger.append(value,chevron);

    select.parentNode.insertBefore(wrapper,select);
    wrapper.append(select,trigger);
    select.classList.add('select-control-native');
    select.hidden=true;
    select.tabIndex=-1;
    select.setAttribute('aria-hidden','true');
    select.dataset.customSelect='1';

    const state={select,wrapper,trigger,value,label,menuId,menu:null,activeIndex:-1};
    states.set(select,state);
    trigger.addEventListener('click',()=>open(state));
    trigger.addEventListener('keydown',event=>onTriggerKeydown(event,state));
    trigger.addEventListener('focus',()=>sync(state));
    select.addEventListener('input',()=>sync(state));
    select.addEventListener('change',()=>sync(state));
    sync(state);
    return state;
  }

  function enhanceAll(root=document){
    if(root instanceof HTMLSelectElement)enhance(root);
    root.querySelectorAll?.('select').forEach(enhance);
  }

  function syncAll(root=document){
    root.querySelectorAll?.('select').forEach(select=>sync(select));
  }

  document.addEventListener('pointerdown',event=>{
    if(!openState)return;
    if(openState.trigger.contains(event.target)||openState.menu?.contains(event.target))return;
    close(openState,{restoreFocus:false});
  });
  document.addEventListener('click',event=>{
    const option=event.target.closest?.('.select-option');
    if(option&&openState?.menu?.contains(option)){
      choose(openState,Number(option.dataset.index));
      return;
    }
    const label=event.target.closest?.('label[for]');
    if(!label)return;
    const select=document.getElementById(label.htmlFor);
    const state=states.get(select);
    if(!state||state.wrapper.contains(event.target))return;
    event.preventDefault();
    state.trigger.focus();
  });
  document.addEventListener('scroll',event=>{
    if(openState&&event.target!==openState.menu&&!openState.menu?.contains(event.target)){
      close(openState,{restoreFocus:false});
    }
  },true);
  window.addEventListener('resize',()=>close(openState,{restoreFocus:false}));
  window.visualViewport?.addEventListener('resize',()=>close(openState,{restoreFocus:false}));
  document.addEventListener('reset',()=>queueMicrotask(()=>syncAll()));

  const observer=new MutationObserver(mutations=>{
    const pending=new Set();
    mutations.forEach(mutation=>{
      mutation.addedNodes.forEach(node=>{
        if(node.nodeType===Node.ELEMENT_NODE)enhanceAll(node);
      });
      const select=mutation.target instanceof HTMLSelectElement
        ?mutation.target:mutation.target.closest?.('select');
      if(select&&states.has(select))pending.add(select);
    });
    pending.forEach(select=>sync(select));
    if(openState&&!openState.select.isConnected)close(openState,{restoreFocus:false});
  });

  function start(){
    enhanceAll();
    observer.observe(document.documentElement,{
      subtree:true,childList:true,characterData:true,attributes:true,
      attributeFilter:['disabled','label','selected','value','class','aria-label']
    });
  }

  window.Cuoti.ui.selects=Object.freeze({enhance,enhanceAll,sync,syncAll,open,close});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});
  else start();
})();
