(function initSettingsUI(){
'use strict';
/* Settings 页的公用界面件：元素拼装、分段按钮、开关、“已保存”回执、弹出菜单、确认框、聚光和背景 inert。
   所有计时器都登记在这里，离开页面时一次清掉。 */
const settingsPage=window.Cuoti.settings;

const timers=new Set();
function later(callback,ms){
  const id=window.setTimeout(()=>{timers.delete(id);callback()},ms);
  timers.add(id);
  return id;
}
function cancel(id){if(id){window.clearTimeout(id);timers.delete(id)}}
function clearTimers(){for(const id of timers)window.clearTimeout(id);timers.clear()}
const nextFrame=callback=>(window.requestAnimationFrame?window.requestAnimationFrame(callback):later(callback,16));
const prefersReduced=()=>Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches);
const iconHTML=(name,className='')=>(typeof window.icon==='function'?window.icon(name,className):'');

function h(tag,attrs,...children){
  const node=document.createElement(tag);
  for(const [key,value] of Object.entries(attrs||{})){
    if(value==null||value===false)continue;
    if(key==='class')node.className=value;
    else if(key==='text')node.textContent=String(value);
    else if(key==='html')node.innerHTML=value;
    else if(key==='dataset')Object.assign(node.dataset,value);
    else if(key.startsWith('on')&&typeof value==='function')node.addEventListener(key.slice(2),value);
    else node.setAttribute(key,value===true?'':String(value));
  }
  for(const child of children.flat(Infinity)){
    if(child==null||child===false||child==='')continue;
    node.append(typeof child==='object'?child:String(child));
  }
  return node;
}
const iconNode=(name,className='')=>h('span',{class:'icon-slot','aria-hidden':'true',html:iconHTML(name,className)});
/* 虚线术语：悬停或键盘聚焦时显示解释 */
const term=(text,tip)=>h('span',{class:'set-term',tabindex:'0','data-tip':tip,'aria-label':`${text}：${tip}`,text});

/* 分段按钮：单选语义，方向键在可用选项间移动并选中 */
function setSeg(container,value){
  container?.querySelectorAll?.('[role="radio"]').forEach(button=>{
    const on=(button.dataset.value??'')===String(value??'');
    button.setAttribute('aria-checked',String(on));
    button.tabIndex=on?0:-1;
  });
}
function bindSeg(container,onChange){
  if(!container||container.dataset.segBound)return;
  container.dataset.segBound='1';
  container.addEventListener('click',event=>{
    const button=event.target.closest('[role="radio"]');
    if(!button||!container.contains(button)||button.disabled||button.getAttribute('aria-checked')==='true')return;
    setSeg(container,button.dataset.value);
    onChange(button.dataset.value??'',button);
  });
  container.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
    const buttons=[...container.querySelectorAll('[role="radio"]:not(:disabled)')];
    const index=buttons.indexOf(document.activeElement);
    if(index<0)return;
    event.preventDefault();
    const step=event.key==='ArrowRight'||event.key==='ArrowDown'?1:-1;
    const next=buttons[(index+step+buttons.length)%buttons.length];
    next.focus();next.click();
  });
}

/* 开关：只表达已保存的状态；保存中用 aria-busy 挡住重复点击 */
function setSwitch(button,on,{disabled=false,busy=false}={}){
  if(!button)return;
  button.setAttribute('aria-checked',String(Boolean(on)));
  button.disabled=Boolean(disabled);
  if(busy)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
}
function bindSwitch(button,onToggle){
  if(!button||button.dataset.switchBound)return;
  button.dataset.switchBound='1';
  button.addEventListener('click',()=>{
    if(button.disabled||button.getAttribute('aria-busy')==='true')return;
    void onToggle(button.getAttribute('aria-checked')!=='true');
  });
}

/* “已保存”回执：即改即存的控件旁闪现 1.6 秒 */
function savedSlot(control){
  if(!control)return null;
  if(control.classList.contains('set-saved'))return control;
  if(control.getAttribute('role')==='switch'){
    let tick=control.querySelector('.set-saved');
    if(!tick){tick=h('span',{class:'set-saved','aria-hidden':'true'});control.append(tick)}
    return tick;
  }
  const next=control.nextElementSibling;
  return next?.classList.contains('set-saved')?next:null;
}
function savedTick(control,text='已保存'){
  const tick=savedSlot(control);
  if(!tick)return;
  tick.innerHTML=iconHTML('check');
  tick.append(text);
  tick.classList.add('is-on');
  cancel(tick._savedTimer);
  tick._savedTimer=later(()=>tick.classList.remove('is-on'),1600);
}

/* 禁用会让按钮丢掉焦点：恢复可用时，若焦点还没被别处接走，就还给它（键盘用户不必重新找位置） */
const focusLost=()=>!document.activeElement||document.activeElement===document.body;
function refocus(button){if(button.isConnected&&focusLost())button.focus({preventScroll:true})}

/* 忙碌按钮：转圈加文字，结束后恢复原内容。同一按钮重叠的忙碌按计数合并，最后一个结束才恢复 */
function busyStart(button,label){
  if(!button)return ()=>{};
  if(!button._settingsBusy){
    button._settingsBusy={count:0,original:[...(button.childNodes||[])],focused:document.activeElement===button};
    button.disabled=true;
    button.setAttribute('aria-busy','true');
    button.replaceChildren(iconNode('loader','icon-spin'),label);
  }
  const holder=button._settingsBusy;
  holder.count+=1;
  let released=false;
  return ()=>{
    if(released)return;
    released=true;
    holder.count-=1;
    if(holder.count>0||button._settingsBusy!==holder)return;
    button._settingsBusy=null;
    button.replaceChildren(...holder.original);
    button.disabled=false;
    button.removeAttribute('aria-busy');
    if(holder.focused)refocus(button);
  };
}
async function busy(button,label,work){
  const restore=busyStart(button,label);
  try{return await work()}finally{restore()}
}
/* 刷新类图标按钮：请求期间转动并禁用 */
function spinning(button,on){
  if(!button)return;
  if(on&&!button.disabled)button._spinFocused=document.activeElement===button;
  button.disabled=Boolean(on);
  button.classList.toggle('is-spinning',Boolean(on));
  if(!on&&button._spinFocused){button._spinFocused=false;refocus(button)}
}

/* 弹出菜单 / 弹层：同一时间只开一个；用视口坐标定位，不被滚动容器裁掉 */
let openMenu=null;
function closeMenu({restore=false}={}){
  if(!openMenu)return;
  const {trigger,node}=openMenu;
  openMenu=null;
  node.remove();
  trigger.setAttribute('aria-expanded','false');
  if(restore&&trigger.isConnected)trigger.focus({preventScroll:true});
}
function placeMenu(trigger,node){
  const box=trigger.getBoundingClientRect();
  const viewportWidth=window.innerWidth||document.documentElement.clientWidth;
  const viewportHeight=window.innerHeight||document.documentElement.clientHeight;
  node.style.position='fixed';
  node.style.top='auto';node.style.bottom='auto';
  node.style.left='auto';
  node.style.right=`${Math.max(8,viewportWidth-box.right)}px`;
  const height=node.offsetHeight||0;
  const below=viewportHeight-box.bottom,above=box.top;
  if(below<height+12&&above>below)node.style.bottom=`${Math.max(8,viewportHeight-box.top+6)}px`;
  else node.style.top=`${Math.min(box.bottom+6,Math.max(8,viewportHeight-height-8))}px`;
  const left=node.getBoundingClientRect().left;
  if(left<8)node.style.right=`${Math.max(8,viewportWidth-box.right+left-8)}px`;
}
function toggleMenu(trigger,build,{className='set-menu',role='menu',label}={}){
  if(openMenu?.trigger===trigger){closeMenu({restore:true});return null}
  closeMenu();
  const node=h('div',{class:className,role,'aria-label':label||trigger.getAttribute('aria-label')||trigger.textContent.trim()});
  (trigger.closest('.set-anchor')||trigger.parentElement).append(node);
  trigger.setAttribute('aria-expanded','true');
  openMenu={trigger,node,place:()=>placeMenu(trigger,node)};
  build(node,()=>closeMenu({restore:true}));
  openMenu.place();
  nextFrame(()=>{
    if(openMenu?.node!==node)return;
    (node.querySelector('[aria-selected="true"]')||node.querySelector('button:not(:disabled)'))?.focus({preventScroll:true});
  });
  return node;
}
/* 菜单内容异步变化后重新定位（例如读取到账号模型） */
function refreshMenu(){openMenu?.place()}
function menuItem({icon='arrowRight',label,hint,danger=false,disabled=false,onClick}){
  return h('button',{type:'button',role:'menuitem',class:danger?'is-danger':null,disabled:disabled||null,
    html:iconHTML(icon),onclick:()=>{closeMenu({restore:true});onClick?.()}},
  h('span',{text:label}),hint?h('small',{text:hint}):null);
}
document.addEventListener('click',event=>{
  if(openMenu&&!openMenu.node.contains(event.target)&&!openMenu.trigger.contains(event.target))closeMenu();
});
document.addEventListener('keydown',event=>{
  if(!openMenu)return;
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeMenu({restore:true});return}
  // 焦点先回到按钮，浏览器的 Tab 再从按钮往后走
  if(event.key==='Tab'){closeMenu({restore:openMenu.node.contains(document.activeElement)});return}
  if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key)||!openMenu.node.contains(document.activeElement))return;
  const items=[...openMenu.node.querySelectorAll('button:not(:disabled)')];
  if(!items.length)return;
  event.preventDefault();
  const index=items.indexOf(document.activeElement);
  const next=event.key==='Home'?items[0]:event.key==='End'?items[items.length-1]
    :items[(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length];
  next.focus();
},true);
document.addEventListener('scroll',event=>{
  if(openMenu&&!openMenu.node.contains(event.target))closeMenu();
},true);
window.addEventListener('resize',()=>closeMenu());

/* 背景 inert：抽屉和讲解播放器打开时锁住页面；通用弹窗关闭会解除 inert，所以关闭后要补回 */
const overlays=new Set();
const modalOpen=()=>!document.getElementById('modal')?.classList.contains('hidden');
function setBackgroundInert(on){
  document.querySelectorAll('.main,.sb').forEach(node=>{
    if(on)node.setAttribute('inert','');else node.removeAttribute('inert');
  });
}
function lockBackground(name){overlays.add(name);setBackgroundInert(true)}
function unlockBackground(name){overlays.delete(name);if(!overlays.size&&!modalOpen())setBackgroundInert(false)}
function restoreOverlayInert(){if(overlays.size)setBackgroundInert(true)}

/* 确认框：在通用弹窗上拼装正文；点叉、点遮罩、按 Esc 都算取消。危险操作默认聚焦“取消” */
function bodyNodes(body){
  return [].concat(body??[]).filter(part=>part!=null&&part!=='')
    .map(part=>typeof part==='string'?h('p',{class:'set-modal-text',text:part}):part);
}
function openDialog({title,body,type,actions,onOpen}){
  return new Promise(resolve=>{
    let settled=false,observer=null;
    const finish=value=>{
      if(settled)return;
      settled=true;
      observer?.disconnect();
      resolve(value);
      restoreOverlayInert();
    };
    const buttons=actions.map((action,index)=>{
      const id=action.primary?'modal-confirm-btn':`set-dialog-action-${index}`;
      return `<button type="button" class="btn btn-sm ${action.className}" id="${id}">${escapeHTML(action.label)}</button>`;
    }).join('');
    showModal(title,'',type,buttons);
    document.getElementById('modal-body')?.replaceChildren(...bodyNodes(body));
    const controls=actions.map((action,index)=>document.getElementById(action.primary?'modal-confirm-btn':`set-dialog-action-${index}`));
    controls.forEach((control,index)=>control?.addEventListener('click',()=>{
      const value=actions[index].value();
      if(value===undefined)return;
      finish(value);
      hideModal();
      restoreOverlayInert();
    }));
    const modal=document.getElementById('modal');
    if(modal&&typeof MutationObserver==='function'){
      observer=new MutationObserver(()=>{
        if(modal.classList.contains('hidden')||!controls[0]?.isConnected)finish(null);
      });
      observer.observe(modal,{attributes:true,attributeFilter:['class'],childList:true,subtree:true});
    }
    if(onOpen)nextFrame(()=>nextFrame(onOpen));
  });
}
async function confirmAction({title,body,type='warning',confirmLabel='确定',cancelLabel='取消',danger=false}){
  const result=await openDialog({title,body,type,actions:[
    {label:cancelLabel,className:'btn-out',value:()=>false},
    {label:confirmLabel,className:danger?'btn-d':'btn-p',primary:!danger,value:()=>true}
  ]});
  return result===true;
}
function promptText({title,body,label,value='',maxLength=500,confirmLabel='保存',emptyMessage='内容不能为空'}){
  const input=h('input',{type:'text',class:'set-modal-input',maxlength:String(maxLength),autocomplete:'off','aria-label':label});
  input.value=value;
  input.addEventListener('keydown',event=>{
    if(event.key==='Enter'){event.preventDefault();document.getElementById('modal-confirm-btn')?.click()}
  });
  return openDialog({title,type:'info',body:[...bodyNodes(body),input],actions:[
    {label:'取消',className:'btn-out',value:()=>null},
    {label:confirmLabel,className:'btn-p',primary:true,value:()=>{
      const text=input.value.trim();
      if(!text){window.showFieldError?.(input,emptyMessage);return undefined}
      return text;
    }}
  ],onOpen:()=>{input.focus();input.select()}});
}
function notice({title,body,type='info'}){
  return openDialog({title,body,type,actions:[{label:'知道了',className:'btn-p',primary:true,value:()=>true}]});
}

/* 聚光：滚到目标、聚焦并点亮两下（减少动态效果时不做平滑滚动） */
function spotlight(target){
  const node=typeof target==='string'?document.querySelector(target):target;
  if(!node)return false;
  const details=node.closest('details');
  if(details&&!details.open)details.open=true;
  const reduced=prefersReduced();
  node.scrollIntoView({behavior:reduced?'auto':'smooth',block:'center'});
  later(()=>{
    node.focus({preventScroll:true});
    node.classList.remove('is-spotlight');
    void node.offsetWidth;
    node.classList.add('is-spotlight');
    later(()=>node.classList.remove('is-spotlight'),1600);
  },reduced?0:420);
  return true;
}

/* 状态广播：右侧目录和窄屏状态条订阅；同一轮里的多次变化合并成一次 */
const listeners=new Set();
let changeQueued=false;
function onChange(listener){listeners.add(listener)}
function changed(){
  if(changeQueued)return;
  changeQueued=true;
  Promise.resolve().then(()=>{
    changeQueued=false;
    listeners.forEach(listener=>{
      try{listener()}catch(error){console.error(error)}
    });
  });
}

function setState(node,{tone='',text=''}){
  if(!node)return;
  node.className=`set-state${tone?` is-${tone}`:''}`;
  node.textContent=text;
}
/* 只切换语气类，保留元素自己的其他类名 */
function setMessage(node,text='',tone=''){
  if(!node)return;
  node.textContent=text;
  for(const name of ['ok','error'])node.classList.toggle(`is-${name}`,tone===name);
}
function unmount(){
  closeMenu();
  clearTimers();
  document.querySelectorAll('#p-settings .set-saved.is-on').forEach(tick=>tick.classList.remove('is-on'));
}

settingsPage.ui=Object.freeze({h,iconHTML,iconNode,term,later,cancel,clearTimers,nextFrame,prefersReduced,
  setSeg,bindSeg,setSwitch,bindSwitch,savedTick,busy,busyStart,spinning,
  toggleMenu,closeMenu,refreshMenu,menuItem,confirm:confirmAction,prompt:promptText,notice,
  lockBackground,unlockBackground,restoreOverlayInert,spotlight,onChange,changed,setState,setMessage,unmount});
})();
