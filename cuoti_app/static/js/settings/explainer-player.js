(function initSettingsExplainerPlayer(){
'use strict';
/* 动画讲解播放器。场景 = draw(t) → SVG 标记：只依据时间 t 画出那一帧，所以能暂停、拖动、跳分镜，
   “减少动态效果”时直接取静帧逐步查看。场景脚本按需加载（先 stage.js，再 <id>.js），加载失败时显示占位画面。 */
const settingsPage=window.Cuoti.settings;
const ui=settingsPage.ui;
const SCRIPTS=settingsPage.explainerScripts||{};
const DESIGN_WIDTH=640;
const SCENE_ROOT='/static/js/settings/explainers/';
const SCENE_QUERY='?bundle=settings-redesign-v1';
const LOAD_TIMEOUT_MS=8000;

/* ── 时间轴（纯函数，供场景与契约测试共用） ── */
const clamp=(value,min,max)=>Math.min(max,Math.max(min,value));
const stepIndexAt=(script,t)=>script.steps.reduce((found,step,index)=>(t>=step.at?index:found),0);
const stepEnd=(script,index)=>(index+1<script.steps.length?script.steps[index+1].at:script.duration);
/* 逐步查看用的静帧：脚本给了 still（该段信息最完整、没有过渡的一刻）就用它，否则取段首稍后 */
const stillTime=(script,index)=>{
  const step=script.steps[index],end=stepEnd(script,index);
  if(Number.isFinite(step.still)&&step.still>=step.at&&step.still<end)return step.still;
  return step.at+Math.min(700,(end-step.at)/2);
};
const clock=ms=>{const seconds=Math.floor(ms/1000);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`};
const timeline=Object.freeze({clamp,stepIndexAt,stepEnd,stillTime,clock});

/* ── 场景注册与按需加载 ── */
const sceneDraws=new Map();
function defineScene(id,draw){if(typeof draw==='function')sceneDraws.set(id,draw)}
const scriptLoads=new Map();
function loadScript(src){
  if(!scriptLoads.has(src)){
    scriptLoads.set(src,new Promise((resolve,reject)=>{
      const node=document.createElement('script');
      node.src=src;node.async=true;
      node.onload=()=>resolve();
      node.onerror=()=>{scriptLoads.delete(src);node.remove();reject(new Error(`无法加载 ${src}`))};
      document.head.appendChild(node);
    }));
  }
  return scriptLoads.get(src);
}
function ensureScene(id){
  if(sceneDraws.has(id))return Promise.resolve(true);
  const loading=loadScript(`${SCENE_ROOT}stage.js${SCENE_QUERY}`)
    .then(()=>loadScript(`${SCENE_ROOT}${id}.js${SCENE_QUERY}`))
    .then(()=>sceneDraws.has(id));
  let timer=0;
  const timeout=new Promise(resolve=>{timer=window.setTimeout(()=>resolve(false),LOAD_TIMEOUT_MS)});
  return Promise.race([loading,timeout]).catch(()=>false).finally(()=>window.clearTimeout(timer));
}

/* 占位画面：场景还在加载或加载失败时，按分镜显示图标和标题 */
function placeholderScene(canvas,script){
  const root=document.createElement('div');
  root.className='xp-ph';
  const frames=script.steps.map(step=>{
    const frame=document.createElement('div');frame.className='xp-ph-step';
    const badge=document.createElement('span');badge.className='xp-ph-icon';badge.innerHTML=ui.iconHTML(step.icon||script.icon);
    const label=document.createElement('strong');label.className='xp-ph-label';label.textContent=step.label;
    frame.append(badge,label);root.appendChild(frame);
    return frame;
  });
  const dots=document.createElement('div');dots.className='xp-ph-dots';
  const dotNodes=script.steps.map(()=>dots.appendChild(document.createElement('i')));
  root.appendChild(dots);
  canvas.appendChild(root);
  return {
    render(t){
      const current=stepIndexAt(script,t);
      frames.forEach((frame,index)=>{
        const start=script.steps[index].at;
        const enter=index===current?clamp((t-start)/380,0,1):0;
        const eased=1-Math.pow(1-enter,3);
        frame.style.opacity=eased.toFixed(3);
        frame.style.visibility=eased>0?'visible':'hidden';
        frame.style.transform=`translateY(${(12*(1-eased)).toFixed(2)}px)`;
      });
      dotNodes.forEach((dot,index)=>dot.classList.toggle('is-on',index===current));
    },
    dispose(){root.remove()}
  };
}

/* 代码绘制的场景：同一帧的标记不变时不重写 DOM */
function svgScene(canvas,draw,fallback){
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 640 360');
  svg.setAttribute('aria-hidden','true');
  svg.setAttribute('focusable','false');
  canvas.appendChild(svg);
  let last='';
  return {
    render(t){
      let markup;
      try{markup=String(draw(t)||'')}catch(error){console.error('Explainer scene failed:',error);fallback();return}
      if(markup!==last){svg.innerHTML=markup;last=markup}
    },
    dispose(){svg.remove()}
  };
}

/* ── 播放器 ── */
const state={id:'',script:null,scene:null,t:0,playing:false,last:0,frame:0,step:-1,board:false,transcript:false,
  trigger:null,dragging:false,resume:false,loading:false,openSerial:0};
let view=null,resizer=null;

function button(className,label,iconName,text=''){
  const node=document.createElement('button');
  node.type='button';node.className=className;
  if(label)node.setAttribute('aria-label',label);
  if(iconName)node.innerHTML=ui.iconHTML(iconName);
  if(text){const span=document.createElement('span');span.textContent=text;node.appendChild(span)}
  return node;
}

function build(){
  const overlay=document.createElement('div');
  overlay.className='xp-overlay';overlay.hidden=true;
  overlay.innerHTML=`<div class="xp-player" role="dialog" aria-modal="true" aria-labelledby="xp-title" aria-describedby="xp-question" tabindex="-1">
    <header class="xp-head"><span class="xp-head-icon" aria-hidden="true"></span><div class="xp-head-text"><h3 id="xp-title"></h3><p id="xp-question"></p></div></header>
    <div class="xp-body">
      <div class="xp-stage" role="img"><div class="xp-canvas"></div><div class="xp-end" hidden></div></div>
      <div class="xp-transcript" tabindex="0"></div>
    </div>
    <p class="xp-caption" aria-live="polite"></p>
    <div class="xp-controls">
      <span class="xp-play-slot"></span>
      <div class="xp-track" role="slider" tabindex="0" aria-label="播放进度" aria-valuemin="0"><div class="xp-track-fill"></div><div class="xp-track-knob"></div></div>
      <span class="xp-time"></span>
    </div>
    <div class="xp-board"><span class="xp-board-prev"></span><span class="xp-board-count"></span><span class="xp-board-next"></span></div>
    <p class="xp-board-note">系统开启了“减少动态效果”，所以先按分镜逐步查看；想看完整动画，点“播放动画”。</p>
    <ol class="xp-chapters" aria-label="分镜"></ol>
  </div>`;
  document.body.appendChild(overlay);
  const q=selector=>overlay.querySelector(selector);
  view={overlay,player:q('.xp-player'),headIcon:q('.xp-head-icon'),title:q('#xp-title'),question:q('#xp-question'),
    stage:q('.xp-stage'),canvas:q('.xp-canvas'),end:q('.xp-end'),transcript:q('.xp-transcript'),caption:q('.xp-caption'),
    track:q('.xp-track'),fill:q('.xp-track-fill'),knob:q('.xp-track-knob'),time:q('.xp-time'),chapters:q('.xp-chapters'),
    boardCount:q('.xp-board-count')};
  view.transcriptToggle=button('xp-tab','','alignLeft','文字版');
  view.transcriptToggle.setAttribute('aria-pressed','false');
  view.closeButton=button('icon-btn','关闭讲解','x');
  q('.xp-head').append(view.transcriptToggle,view.closeButton);
  view.play=button('xp-play','播放','play');
  q('.xp-play-slot').replaceWith(view.play);
  view.prev=button('btn btn-out btn-sm','',null,'‹ 上一步');
  view.next=button('btn btn-out btn-sm','',null,'下一步 ›');
  view.playFromBoard=button('btn btn-p btn-sm','','play','播放动画');
  q('.xp-board-prev').replaceWith(view.prev);
  const nextGroup=document.createElement('span');nextGroup.className='xp-board-actions';nextGroup.append(view.next,view.playFromBoard);
  q('.xp-board-next').replaceWith(nextGroup);

  view.closeButton.addEventListener('click',close);
  overlay.addEventListener('click',event=>{if(event.target===overlay)close()});
  view.play.addEventListener('click',()=>(state.playing?pause():play()));
  view.transcriptToggle.addEventListener('click',()=>setTranscript(!state.transcript));
  view.prev.addEventListener('click',()=>boardStep(-1));
  view.next.addEventListener('click',()=>boardStep(1));
  view.playFromBoard.addEventListener('click',()=>{setBoard(false);seek(0);play();view.play.focus()});
  view.chapters.addEventListener('click',event=>{
    const target=event.target.closest('[data-step]');
    if(!target||!state.script)return;
    const index=Number(target.dataset.step);
    // 在文字版里点分镜：回到画面并跳到那一段
    if(state.transcript)setTranscript(false);
    if(state.board){seek(stillTime(state.script,index));return}
    seek(state.script.steps[index].at);play();
  });
  bindTrack();
  overlay.addEventListener('keydown',onKey);
  if(typeof ResizeObserver==='function'){resizer=new ResizeObserver(fit);resizer.observe(view.stage)}
  else window.addEventListener('resize',fit);
}

function bindTrack(){
  const seekAtPointer=event=>{
    const box=view.track.getBoundingClientRect();
    seek(clamp((event.clientX-box.left)/Math.max(1,box.width),0,1)*state.script.duration);
  };
  view.track.addEventListener('pointerdown',event=>{
    if(!state.script)return;
    state.dragging=true;state.resume=state.playing;pause();
    view.track.setPointerCapture?.(event.pointerId);seekAtPointer(event);
  });
  view.track.addEventListener('pointermove',event=>{if(state.dragging)seekAtPointer(event)});
  const release=()=>{if(!state.dragging)return;state.dragging=false;if(state.resume&&state.t<state.script.duration)play()};
  view.track.addEventListener('pointerup',release);
  view.track.addEventListener('pointercancel',release);
}

function fit(){
  if(!view)return;
  const width=view.stage.clientWidth||DESIGN_WIDTH;
  view.canvas.style.setProperty('--k',(width/DESIGN_WIDTH).toFixed(4));
}

/* “去试试”的目标：脚本里可以给几个候选选择器，取第一个当前可见的 */
function resolveTarget(target){
  for(const selector of [].concat(target||[])){
    const node=document.querySelector(selector);
    if(node&&node.getClientRects?.().length)return node;
  }
  return null;
}
function goButton(script,className){
  const go=button(className,'',null,`${script.cta.label} →`);
  go.addEventListener('click',()=>{
    const target=resolveTarget(script.cta.target);
    close();
    if(target)ui.spotlight(target);
  });
  return go;
}

function renderChapters(script){
  view.chapters.replaceChildren(...script.steps.map((step,index)=>{
    const item=document.createElement('li');
    const control=document.createElement('button');
    control.type='button';control.dataset.step=String(index);
    const number=document.createElement('span');number.textContent=String(index+1);
    control.append(number,document.createTextNode(step.label));
    item.appendChild(control);
    return item;
  }));
  view.track.querySelectorAll('.xp-mark').forEach(mark=>mark.remove());
  script.steps.slice(1).forEach(step=>{
    const mark=document.createElement('span');mark.className='xp-mark';
    mark.style.left=`${(step.at/script.duration*100).toFixed(2)}%`;
    view.track.insertBefore(mark,view.knob);
  });
}

function renderTranscript(script,ctaReady){
  const list=document.createElement('ol');
  script.steps.forEach(step=>{
    const item=document.createElement('li');
    const label=document.createElement('b');label.textContent=step.label;
    item.append(label,document.createTextNode(step.caption));
    list.appendChild(item);
  });
  const nodes=[list];
  if(script.glossary?.length){
    const heading=document.createElement('h4');heading.textContent='名词解释';
    const terms=document.createElement('dl');
    script.glossary.forEach(([word,meaning])=>{
      const dt=document.createElement('dt');dt.textContent=word;
      const dd=document.createElement('dd');dd.textContent=meaning;
      terms.append(dt,dd);
    });
    nodes.push(heading,terms);
  }
  if(script.note){const note=document.createElement('p');note.className='xp-note';note.textContent=script.note;nodes.push(note)}
  if(ctaReady){const row=document.createElement('p');row.className='xp-transcript-cta';row.appendChild(goButton(script,'btn btn-p btn-sm'));nodes.push(row)}
  view.transcript.replaceChildren(...nodes);
}

function renderEnd(script,ctaReady){
  const replay=button('btn btn-out','','rotateCcw','重播');
  replay.addEventListener('click',()=>{seek(0);play();view.play.focus()});
  view.end.replaceChildren(replay,...(ctaReady?[goButton(script,'btn btn-p')]:[]));
  view.boardGo?.remove();
  view.boardGo=ctaReady?goButton(script,'btn btn-p btn-sm'):null;
  if(view.boardGo){view.boardGo.hidden=true;view.playFromBoard.before(view.boardGo)}
}

function sceneFor(id,script){
  const draw=sceneDraws.get(id);
  if(!draw)return placeholderScene(view.canvas,script);
  return svgScene(view.canvas,draw,()=>{
    if(state.script!==script)return;
    state.scene?.dispose?.();
    state.scene=placeholderScene(view.canvas,script);
    state.scene.render(state.t);
  });
}

async function open(id,{trigger=null}={}){
  const script=SCRIPTS[id];
  if(!script)return;
  if(!view)build();
  const serial=++state.openSerial;
  const wasOpen=!view.overlay.hidden;
  pause();
  state.scene?.dispose?.();state.scene=null;
  view.canvas.replaceChildren();
  Object.assign(state,{id,script,t:0,step:-1,trigger:wasOpen?state.trigger:(trigger||document.activeElement)});
  view.headIcon.innerHTML=ui.iconHTML(script.icon);
  view.title.textContent=script.title;
  view.question.textContent=script.question;
  view.stage.setAttribute('aria-label',`${script.title}动画讲解，约 ${Math.round(script.duration/1000)} 秒`);
  view.track.setAttribute('aria-valuemax',String(Math.round(script.duration/1000)));
  const ctaReady=Boolean(script.cta&&resolveTarget(script.cta.target));
  renderChapters(script);renderTranscript(script,ctaReady);renderEnd(script,ctaReady);
  view.overlay.hidden=false;
  ui.lockBackground('explainer');
  if(!wasOpen)window.pushHistoryLayer?.('explainer',()=>closeView());
  fit();
  setTranscript(false);
  setBoard(ui.prefersReduced());
  state.loading=!sceneDraws.has(id);
  state.scene=sceneFor(id,script);
  seek(state.board?stillTime(script,0):0);
  (state.board?view.next:view.play).focus({preventScroll:true});
  if(state.loading){
    const ready=await ensureScene(id);
    if(serial!==state.openSerial||view.overlay.hidden)return;
    state.loading=false;
    if(ready){state.scene.dispose();state.scene=sceneFor(id,script)}
    seek(state.t);
  }
  // 用户在加载期间没动过时才自动播放
  if(!state.board&&!state.transcript&&!state.playing&&state.t===0)play();
}

/* 只收起界面；历史层由 close 经返回键消化，离开页面时直接收起 */
function closeView({restore=true}={}){
  if(!view||view.overlay.hidden)return;
  pause();
  state.openSerial+=1;
  view.overlay.hidden=true;
  ui.unlockBackground('explainer');
  state.scene?.dispose?.();state.scene=null;
  const trigger=state.trigger;state.trigger=null;
  if(restore&&trigger?.isConnected)trigger.focus({preventScroll:true});
}
function close(){
  if(!view||view.overlay.hidden)return;
  if(!window.dismissHistoryLayer?.('explainer'))closeView();
}

function syncPlayButton(){
  view.play.innerHTML=ui.iconHTML(state.playing?'pause':'play');
  view.play.setAttribute('aria-label',state.playing?'暂停':'播放');
}
function play(){
  if(!state.script||state.board)return;
  if(state.t>=state.script.duration)seek(0);
  state.playing=true;state.last=performance.now();
  view.end.hidden=true;
  syncPlayButton();
  cancelAnimationFrame(state.frame);
  state.frame=requestAnimationFrame(tick);
}
function pause(){
  state.playing=false;
  if(state.frame)cancelAnimationFrame(state.frame);
  state.frame=0;
  if(view)syncPlayButton();
}
function tick(now){
  if(!state.playing)return;
  // 切走后回来不跳帧：单帧最多推进 100ms
  const delta=Math.min(100,Math.max(0,now-state.last));
  state.last=now;
  seek(state.t+delta);
  if(state.t>=state.script.duration){
    state.playing=false;state.frame=0;syncPlayButton();
    view.end.hidden=false;
    (view.end.querySelector('.btn-p')||view.end.querySelector('.btn'))?.focus({preventScroll:true});
    return;
  }
  state.frame=requestAnimationFrame(tick);
}

function syncBoard(){
  if(!state.script)return;
  const last=state.step===state.script.steps.length-1;
  if(view.boardGo)view.boardGo.hidden=!(state.board&&last);
}
function seek(t){
  const script=state.script;
  if(!script||!state.scene)return;
  state.t=clamp(t,0,script.duration);
  state.scene.render(state.t);
  const index=stepIndexAt(script,state.t);
  if(index!==state.step){
    state.step=index;
    view.caption.textContent=script.steps[index].caption;
    view.chapters.querySelectorAll('button').forEach((control,i)=>{
      if(i===index)control.setAttribute('aria-current','step');else control.removeAttribute('aria-current');
      control.classList.toggle('is-done',i<index);
    });
    view.boardCount.textContent=`第 ${index+1} / ${script.steps.length} 步`;
    view.prev.disabled=index===0;view.next.disabled=index===script.steps.length-1;
    syncBoard();
  }
  const ratio=state.t/script.duration*100;
  view.fill.style.width=`${ratio.toFixed(2)}%`;
  view.knob.style.left=`${ratio.toFixed(2)}%`;
  view.time.textContent=`${clock(state.t)} / ${clock(script.duration)}`;
  view.track.setAttribute('aria-valuenow',String(Math.round(state.t/1000)));
  view.track.setAttribute('aria-valuetext',`${clock(state.t)}，${script.steps[index].label}`);
  if(state.t<script.duration)view.end.hidden=true;
}

function setBoard(enabled){
  state.board=enabled;
  view.player.classList.toggle('is-board',enabled);
  if(enabled){pause();view.end.hidden=true}
  syncBoard();
}
function boardStep(direction){
  if(!state.script)return;
  const index=clamp(state.step+direction,0,state.script.steps.length-1);
  seek(stillTime(state.script,index));
  if(document.activeElement?.disabled)(direction<0?view.next:(view.boardGo&&!view.boardGo.hidden?view.boardGo:view.prev)).focus();
}
function setTranscript(enabled){
  state.transcript=enabled;
  view.player.classList.toggle('is-transcript',enabled);
  view.transcriptToggle.setAttribute('aria-pressed',String(enabled));
  if(enabled){pause();view.transcript.focus({preventScroll:true})}
}

function onKey(event){
  if(event.key==='Escape'){event.preventDefault();close();return}
  if(event.key==='Tab'){trapFocus(event);return}
  if(!state.script)return;
  if(event.target===view.track&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
    event.preventDefault();
    seek({ArrowLeft:state.t-1000,ArrowRight:state.t+1000,Home:0,End:state.script.duration}[event.key]);
    return;
  }
  if(event.target.closest('button,[role="slider"]')||state.transcript)return;
  if(event.key===' '||event.key==='k'){event.preventDefault();if(!state.board)(state.playing?pause():play())}
  else if(event.key==='ArrowRight'||event.key==='ArrowLeft'){
    event.preventDefault();
    const index=clamp(state.step+(event.key==='ArrowRight'?1:-1),0,state.script.steps.length-1);
    seek(state.board?stillTime(state.script,index):state.script.steps[index].at);
  }
}
function trapFocus(event){
  const focusable=[...view.player.querySelectorAll('button:not(:disabled),[tabindex="0"]')]
    .filter(node=>node.offsetParent!==null&&!node.closest('[hidden]'));
  if(!focusable.length)return;
  const first=focusable[0],last=focusable[focusable.length-1];
  if(!view.player.contains(document.activeElement)||document.activeElement===view.player){
    event.preventDefault();(event.shiftKey?last:first).focus();
  }else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}
}
document.addEventListener('visibilitychange',()=>{if(document.hidden&&state.playing)pause()});

/* 卡头入口：文字与无障碍名称都从脚本生成，时长永远和动画一致 */
function mountTriggers(root=document){
  root.querySelectorAll('[data-explainer-open]').forEach(trigger=>{
    const script=SCRIPTS[trigger.dataset.explainerOpen];
    if(!script){trigger.hidden=true;return}
    const seconds=Math.round(script.duration/1000);
    trigger.innerHTML=`<span class="xp-trigger-dot" aria-hidden="true">${ui.iconHTML('play')}</span><span>${seconds} 秒看懂</span>`;
    trigger.setAttribute('aria-label',`观看动画讲解：${script.title}（约 ${seconds} 秒）`);
    trigger.setAttribute('aria-haspopup','dialog');
    trigger.title=script.question;
    trigger.hidden=false;
    if(trigger.dataset.explainerBound)return;
    trigger.dataset.explainerBound='1';
    trigger.addEventListener('click',()=>void open(trigger.dataset.explainerOpen,{trigger}));
  });
}

function unmount(){closeView({restore:false})}

settingsPage.explainer=Object.freeze({open,close,unmount,mountTriggers,defineScene,timeline,scripts:SCRIPTS,
  play,pause,seek(t){if(state.script)seek(t)},hasScene:id=>sceneDraws.has(id),
  snapshot:()=>({id:state.id||null,t:state.t,step:state.step,playing:state.playing,board:state.board,transcript:state.transcript,loading:state.loading})});
})();
