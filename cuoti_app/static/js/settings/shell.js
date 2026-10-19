(function initSettingsShell(){
'use strict';
/* 页面外壳：宽屏右侧目录、窄屏顶部状态胶囊、点击跳到卡片、滚动时高亮当前卡片。
   状态文字由各卡片模块提供；链接只建一次，之后原地更新，不会抢走焦点。 */
const settingsPage=window.Cuoti.settings;
const ui=settingsPage.ui;
const {h}=ui;
const el=id=>document.getElementById(id);
const EMPTY=Object.freeze({tone:'',text:''});
const from=(module,method='status')=>()=>{
  try{return settingsPage[module]?.[method]?.()||EMPTY}catch(error){console.error(error);return EMPTY}
};
const STATUS=Object.freeze({
  ai:from('connection'),routing:from('routing'),usage:from('diagnostics','usageStatus'),memory:from('memory'),
  bank:from('maintenance','bankStatus'),semantic:from('maintenance','semanticStatus'),health:from('diagnostics','healthStatus'),
  backup:()=>EMPTY
});
const RAIL=Object.freeze([
  {group:'AI',items:[['card-ai','AI 服务','ai'],['card-routing','模型分工','routing'],['card-usage','使用情况','usage'],['card-memory','AI 记忆','memory']]},
  {group:'题库',items:[['card-bank','普通题库索引','bank'],['card-semantic','语义索引','semantic']]},
  {group:'系统',items:[['card-health','运行状态','health'],['card-backup','数据备份','backup']]}
]);
const SUMMARY=Object.freeze([['card-ai','AI 服务','ai'],['card-bank','题库索引','bank'],['card-semantic','语义索引','semantic'],['card-health','运行状态','health']]);
let current='card-ai',observer=null,built=false,pinned=false;

/* href 指向本页路由：不会改动地址里的页面，脚本失效时点了也留在设置页 */
function build(){
  const rail=el('set-rail'),summary=el('set-summary');
  rail?.replaceChildren(...RAIL.flatMap(group=>[
    h('div',{class:'set-rail-label',text:group.group}),
    ...group.items.map(([id,label,key])=>h('a',{href:'#/settings',dataset:{target:id,status:key}},h('i',{'aria-hidden':'true'}),label,h('small')))
  ]));
  summary?.replaceChildren(...SUMMARY.map(([id,label,key])=>h('a',{href:'#/settings',dataset:{target:id,status:key}},
    h('i',{'aria-hidden':'true'}),h('b',{text:label}),h('span'))));
  built=true;
}

function refresh(){
  if(!built)build();
  document.querySelectorAll('#set-rail a[data-status],#set-summary a[data-status]').forEach(link=>{
    const status=STATUS[link.dataset.status]?.()||EMPTY;
    const rail=link.closest('#set-rail');
    for(const tone of ['ok','warn','error','busy'])link.classList.toggle(`tone-${tone}`,status.tone===tone);
    const text=link.querySelector(rail?'small':'span');
    if(text)text.textContent=status.text||(rail?'':'—');
    if(rail){
      const on=link.dataset.target===current;
      link.classList.toggle('is-current',on);
      if(on)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');
    }
  });
}

function setCurrent(id){
  if(id===current)return;
  current=id;
  refresh();
}

function jump(event){
  const link=event.target.closest('a[data-target]');
  if(!link)return;
  event.preventDefault();
  const card=el(link.dataset.target);
  if(!card)return;
  card.scrollIntoView({behavior:ui.prefersReduced()?'auto':'smooth',block:'start'});
  card.classList.remove('is-flash');void card.offsetWidth;card.classList.add('is-flash');
  ui.later(()=>card.classList.remove('is-flash'),1300);
  const heading=card.querySelector('h2');
  if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true})}
  pinned=true;
  setCurrent(card.id);
}
el('set-rail')?.addEventListener('click',jump);
el('set-summary')?.addEventListener('click',jump);

/* 滚动定位：卡片顶边已越过视口 30% 锚线的，最靠后的一张算“当前”。
   点目录跳转后先认所点的卡片（并排的两张、页尾滚不到锚线的也不会被改掉），直到用户自己滚动 */
const ANCHOR=.3;
const USER_SCROLL=Object.freeze(['wheel','touchstart','keydown']);
const unpin=()=>{pinned=false};
function pick(cards){
  if(pinned||!cards.length)return;
  const line=window.innerHeight*ANCHOR;
  let found=cards[0];
  for(const card of cards)if(card.getBoundingClientRect().top<=line)found=card;
  setCurrent(found.id);
}
function watchScroll(){
  if(observer||typeof IntersectionObserver!=='function')return;
  const cards=[...document.querySelectorAll('#p-settings .set-card')];
  observer=new IntersectionObserver(()=>pick(cards),{rootMargin:`0px 0px -${Math.round((1-ANCHOR)*100)}% 0px`});
  cards.forEach(card=>observer.observe(card));
  USER_SCROLL.forEach(type=>window.addEventListener(type,unpin,{passive:true}));
}

ui.onChange(()=>{if(settingsPage.state.active)refresh()});

function mount(){refresh();watchScroll()}
function unmount(){
  if(observer)USER_SCROLL.forEach(type=>window.removeEventListener(type,unpin));
  observer?.disconnect();observer=null;pinned=false;
}

settingsPage.shell=Object.freeze({mount,unmount,refresh});
})();
