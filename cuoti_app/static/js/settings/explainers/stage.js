(function initExplainerStage(){
'use strict';
/* 讲解动画的绘图工具箱。每段动画是一次“界面演示”：按真实设置卡片画出界面，鼠标指针移动、点击、输入，
   界面随之变化，旁边的深色标注就近说明正在发生什么。画布 640×360，卡片占满画布（四周留 12）。
   约定：颜色只引用页面的 CSS 变量；文字不小于 20；不用随机数和当前时间，同一时刻永远画出同一帧。 */
const settingsPage=window.Cuoti.settings;

const C=Object.freeze({
  card:'var(--card)',bg:'var(--bg)',surface:'var(--surface)',soft:'var(--surface-soft)',
  border:'var(--border)',text:'var(--text)',muted:'var(--muted)',placeholder:'var(--placeholder)',
  accent:'var(--accent)',accentLight:'var(--accent-light)',accentSoft:'var(--accent-soft)',accentStrong:'var(--accent-strong)',
  green:'var(--green)',greenSoft:'var(--success-soft)',greenLine:'var(--success-border)',pass:'var(--chart-pass)',
  mastered:'var(--chart-mastered)',again:'var(--chart-again)',track:'var(--chart-track)',
  red:'var(--red)',redSoft:'var(--danger-soft)',redLine:'var(--danger-border)',
  warn:'var(--warn-strong)',warnSoft:'var(--warn-soft)',warnLine:'var(--warn-border)',warnText:'var(--warn-text)',
  blue:'var(--blue)',blueSoft:'var(--info-soft)',orange:'var(--orange)',
  g1:'var(--gray-100)',g2:'var(--gray-200)',g3:'var(--gray-300)',g4:'var(--gray-400)',g5:'var(--gray-500)',
  gold:'var(--xp-gold)',range:'var(--xp-range)'
});

/* ── 数值与时间 ── */
const clamp=(value,min=0,max=1)=>Math.min(max,Math.max(min,value));
const f=value=>{const rounded=Math.round(value*10)/10;return Object.is(rounded,-0)?0:rounded};
const seg=(t,start,duration)=>clamp((t-start)/duration);
const lerp=(a,b,p)=>a+(b-a)*p;
/* CSS cubic-bezier：牛顿迭代求 x→t，再取 y */
function bezier(x1,y1,x2,y2){
  const curve=(a,b,u)=>3*a*u*(1-u)*(1-u)+3*b*u*u*(1-u)+u*u*u;
  const slope=(a,b,u)=>3*a*(1-u)*(1-u)+6*(b-a)*u*(1-u)+3*(1-b)*u*u;
  return p=>{
    if(p<=0)return 0;
    if(p>=1)return 1;
    let u=p;
    for(let i=0;i<8;i+=1){const d=slope(x1,x2,u);if(Math.abs(d)<1e-6)break;u=clamp(u-(curve(x1,x2,u)-p)/d)}
    return curve(y1,y2,u);
  };
}
const ease=Object.freeze({
  out:p=>1-Math.pow(1-p,3),
  in:p=>p*p*p,
  inOut:p=>(p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2),
  // 两端取精确值：公式在 p=0 处会算出极小的正数，让“尚未出现”的元素被当成已出现
  back:p=>{if(p<=0)return 0;if(p>=1)return 1;const c1=1.70158,c3=c1+1;return 1+c3*Math.pow(p-1,3)+c1*Math.pow(p-1,2)},
  // 指针移动：先快后稳，和参考演示的 cubic-bezier(.3,.7,.3,1) 一致
  cursor:bezier(.3,.7,.3,1)
});
/* 在 [from,to) 内可见：两端各用 duration 毫秒淡入淡出 */
const show=(t,from,to=Infinity,duration=240)=>Math.min(seg(t,from,duration),to===Infinity?1:1-seg(t,to-duration,duration));
/* 关键帧插值：keys = [[时间, 值1, 值2, …], …]，段内按 easing 过渡 */
function track(t,keys,easing=ease.inOut){
  if(t<=keys[0][0])return keys[0].slice(1);
  for(let index=1;index<keys.length;index+=1){
    const [t1,...to]=keys[index],[t0,...from]=keys[index-1];
    if(t<=t1){const p=easing(seg(t,t0,Math.max(1,t1-t0)));return from.map((value,i)=>lerp(value,to[i],p))}
  }
  return keys[keys.length-1].slice(1);
}
/* 二次贝塞尔：卡片沿弧线换位置 */
const arc=(from,control,to,u)=>[0,1].map(i=>(1-u)*(1-u)*from[i]+2*(1-u)*u*control[i]+u*u*to[i]);
/* 按下：at 前 120ms 按下，之后 160ms 弹起；用于按钮缩放和指针 */
const pressAt=(t,at)=>(at-t>0?seg(t,at-120,120):1-seg(t,at,160));
const pressAny=(t,times)=>Math.max(0,...times.map(at=>pressAt(t,at)));
/* 逐字输入：从 at 开始每 step 毫秒出一个字 */
const typed=(t,at,value,step=90)=>(t<at?'':[...value].slice(0,Math.floor((t-at)/step)+1).join(''));
/* 光标闪烁：输入进行中常亮，停下后按 1 秒周期闪 */
const caretOn=(t,since)=>t<since||Math.floor((t-since)/500)%2===0;

/* ── SVG 基元 ── */
const esc=value=>String(value).replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
const op=value=>(value==null||value>=1?null:String(Math.max(0,Math.round(value*100)/100)));
function attrs(map){
  let out='';
  for(const [key,value] of Object.entries(map)){
    if(value==null||value===false||value==='')continue;
    out+=` ${key}="${typeof value==='number'?f(value):esc(value)}"`;
  }
  return out;
}
const node=(tag,map,inner)=>(inner==null?`<${tag}${attrs(map)}/>`:`<${tag}${attrs(map)}>${inner}</${tag}>`);
const hidden=o=>o!=null&&o<=0.005;
const stroked=(o,width)=>({stroke:o.stroke,'stroke-width':o.stroke?(o.sw??width):null,'stroke-dasharray':o.dash,'stroke-dashoffset':o.dashOffset});
const rect=(x,y,w,h,o={})=>(hidden(o.o)?'':node('rect',{x,y,width:Math.max(0,w),height:Math.max(0,h),rx:o.r||null,fill:o.fill??'none',...stroked(o,1.5),opacity:op(o.o)}));
const circle=(cx,cy,r,o={})=>(hidden(o.o)?'':node('circle',{cx,cy,r:Math.max(0,r),fill:o.fill??'none',...stroked(o,1.5),opacity:op(o.o)}));
const line=(x1,y1,x2,y2,o={})=>(hidden(o.o)?'':node('line',{x1,y1,x2,y2,stroke:o.stroke??C.border,'stroke-width':o.sw??2,'stroke-linecap':o.cap??'round','stroke-dasharray':o.dash,'stroke-dashoffset':o.dashOffset,opacity:op(o.o)}));
const path=(d,o={})=>(hidden(o.o)?'':node('path',{d,fill:o.fill??'none',...stroked(o,2),'stroke-linecap':o.stroke?'round':null,'stroke-linejoin':o.stroke?'round':null,opacity:op(o.o)}));
function text(x,y,value,o={}){
  if(hidden(o.o))return '';
  return node('text',{x,y,'font-size':o.size??20,'font-weight':o.weight??700,fill:o.fill??C.text,'text-anchor':o.anchor,
    'dominant-baseline':'central','letter-spacing':o.ls,opacity:op(o.o)},esc(value));
}
/* 组：平移 → 旋转 → 缩放；完全透明时整组不画 */
function g(inner,o={}){
  if(!inner||hidden(o.o))return '';
  const parts=[];
  if(o.x||o.y)parts.push(`translate(${f(o.x||0)} ${f(o.y||0)})`);
  if(o.r)parts.push(`rotate(${f(o.r)})`);
  if(o.s!=null&&Math.abs(o.s-1)>0.0005)parts.push(`scale(${Math.round(o.s*1000)/1000})`);
  return node('g',{class:o.cls,'data-callout':o.callout,transform:parts.join(' '),opacity:op(o.o)},inner);
}

/* 文字宽度估算（微软雅黑）：中文和全角符号按 1 个字号，其余按字符类别 */
function textWidth(value,size=20){
  let width=0;
  for(const ch of String(value)){
    const code=ch.codePointAt(0);
    if(code>=0x2E80||(code>=0x2190&&code<=0x21FF)||ch==='…'||ch==='—'||ch==='✓')width+=1;
    else if(ch===' ')width+=.3;
    else if(/[A-Z0-9%#]/.test(ch))width+=.6;
    else if(/[a-z]/.test(ch))width+=.52;
    else width+=.38;
  }
  return width*size;
}

/* 图标：复用页面图标库的路径，嵌成内层 SVG */
const iconCache=new Map();
function iconPaths(name){
  if(!iconCache.has(name)){
    const markup=typeof window.icon==='function'?String(window.icon(name)):'';
    iconCache.set(name,markup.replace(/^<svg[^>]*>/,'').replace(/<\/svg>\s*$/,''));
  }
  return iconCache.get(name);
}
function glyph(name,cx,cy,size,color,o={}){
  if(hidden(o.o))return '';
  return node('svg',{x:cx-size/2,y:cy-size/2,width:size,height:size,viewBox:'0 0 24 24',fill:o.fill??'none',stroke:color,
    'stroke-width':o.sw??2,'stroke-linecap':'round','stroke-linejoin':'round',overflow:'visible',opacity:op(o.o)},iconPaths(name));
}
const spin=(name,cx,cy,size,color,t)=>g(glyph(name,0,0,size,color),{x:cx,y:cy,r:(t%900)/900*360});

/* ── 界面件：外观对应设置页的真实样式（字号按画布放大） ── */
/* 卡片：白底细边，下方一层很淡的投影 */
function card(x,y,w,h,o={}){
  const r=o.r??12;
  return (o.shadow===false?'':rect(x,y+3,w,h,{r,fill:C.accent,o:.07*(o.o??1)}))
    +rect(x,y,w,h,{r,fill:o.fill??C.card,stroke:o.stroke??C.border,sw:o.sw??1.5,dash:o.dash,o:o.o});
}
/* 设置卡片本身：图标 + 标题；dy 用于“滚动页面” */
const PANEL=Object.freeze({x:12,y:12,w:616,h:336,headY:46,left:36,right:604});
function panel(icon,title,o={}){
  const dy=o.dy??0,h=o.h??PANEL.h;
  return card(PANEL.x,PANEL.y+dy,PANEL.w,h,{r:16})
    +glyph(icon,PANEL.left+4,PANEL.headY+dy,24,C.accent)+text(PANEL.left+26,PANEL.headY+1+dy,title,{size:24,weight:800,fill:C.accentStrong});
}

const chipWidth=(label,o={})=>(o.pad??14)*2+(o.icon?(o.iconSize??22)+8:0)+textWidth(label,o.size??20);
/* 胶囊标签：x 为左端（align:'center' 时为中心，'end' 时为右端），y 为垂直中心 */
function chip(x,y,label,o={}){
  if(hidden(o.o))return '';
  const size=o.size??20,h=o.h??40,pad=o.pad??14,iconSize=o.iconSize??22,gap=o.icon?8:0;
  const w=o.w??chipWidth(label,o);
  const left=o.align==='center'?x-w/2:o.align==='end'?x-w:x;
  const color=o.color??C.text;
  const start=o.w?left+(w-((o.icon?iconSize+gap:0)+textWidth(label,size)))/2:left+pad;
  return g(rect(left,y-h/2,w,h,{r:o.r??h/2,fill:o.fill??C.card,stroke:o.stroke??C.border,sw:o.sw??1.5,dash:o.dash})
    +(o.icon?glyph(o.icon,start+iconSize/2,y,iconSize,o.iconColor??color):'')
    +text(start+(o.icon?iconSize+gap:0),y+1,label,{size,fill:color,weight:o.weight??700}),{o:o.o});
}

/* 状态徽标（.set-state）：圆点 + 文字；x 为右端 */
const PILL_TONES=Object.freeze({
  ok:{fill:C.greenSoft,color:C.green},warn:{fill:C.warnSoft,color:C.warnText},error:{fill:C.redSoft,color:C.red},
  busy:{fill:C.blueSoft,color:C.blue},idle:{fill:C.g1,color:C.muted}
});
const pillWidth=label=>36+textWidth(label);
function pill(x,cy,label,tone='idle',o={}){
  const spec=PILL_TONES[tone]||PILL_TONES.idle,w=pillWidth(label);
  const left=o.align==='start'?x:x-w;
  // 进行中的圆点按时间明暗呼吸（.set-state.is-busy）
  const dot=tone==='busy'?.35+.65*Math.abs(Math.cos((o.t??0)/380)):1;
  return g(rect(left,cy-17,w,34,{r:17,fill:spec.fill})+circle(left+16,cy,4.5,{fill:spec.color,o:dot})
    +text(left+27,cy+1,label,{fill:spec.color}),{s:o.s,o:o.o});
}
/* “已保存”回执（.set-saved）：绿色对勾 + 文字；x 为右端 */
function saved(x,cy,label,p){
  if(p<=0)return '';
  const w=textWidth(label)+30;
  return g(glyph('check',x-w+10,cy,20,C.green,{sw:2.6})+text(x-w+26,cy+1,label,{fill:C.green}),{o:p});
}

/* 按钮（.btn，胶囊形）：kind p 主色 / out 描边 / s 绿 / d 红 / txt 文字按钮；以中心定位。
   iconAfter 时图标放在文字后面（如“查看和管理画像 ›”）；busy 时图标转圈（进行中） */
const buttonWidth=(label,o={})=>(o.pad??18)*2+(o.icon?28:0)+textWidth(label,o.size??20);
function button(cx,cy,label,o={}){
  const kind=o.kind??'p',h=o.h??42,w=o.w??buttonWidth(label,o);
  const filled={p:C.accent,s:C.green,d:C.red}[kind];
  const disabled=Boolean(o.disabled);
  const fill=disabled&&filled?C.g2:(filled??(kind==='txt'?'none':C.card));
  const color=disabled?C.muted:filled?C.card:kind==='txt'?C.accent:C.text;
  const start=-w/2+(w-((o.icon?28:0)+textWidth(label)))/2;
  const press=o.press??0;
  const iconX=o.iconAfter?start+textWidth(label)+17:start+11,labelX=o.iconAfter?start:start+(o.icon?28:0);
  return g((filled&&!disabled?rect(-w/2,-h/2+2,w,h,{r:h/2,fill,o:.18}):'')
    +rect(-w/2,-h/2,w,h,{r:h/2,fill:o.active&&!filled?C.accentSoft:fill,stroke:filled||kind==='txt'?null:(o.active?C.accentLight:C.g3),sw:1.5})
    +(press>0&&kind!=='txt'?rect(-w/2,-h/2,w,h,{r:h/2,fill:C.accentStrong,o:.16*press}):'')
    +(o.icon?(o.busy?spin(o.icon,iconX,0,22,color,o.t??0):glyph(o.icon,iconX,0,22,color)):'')
    +text(labelX,1,label,{fill:color}),{x:cx,y:cy,s:1-.05*press,o:o.o});
}
/* 图标按钮（.set-icon-btn）：方形圆角；spin 为旋转角度 */
function iconButton(cx,cy,name,o={}){
  const size=o.size??40,press=o.press??0;
  return g(rect(-size/2,-size/2,size,size,{r:size*.3,fill:o.active?C.accentSoft:C.card,stroke:o.active?C.accentLight:C.border,sw:1.5})
    +g(glyph(name,0,0,Math.round(size*.55),o.active?C.accentStrong:(o.color??C.muted)),{r:o.spin}),{x:cx,y:cy,s:1-.06*press,o:o.o});
}
/* 输入框：focus 0–1 时描边转为主色；caret 为 true 时在文字末尾画光标 */
function field(x,cy,w,o={}){
  const h=o.h??44,focus=o.focus??0;
  const value=o.value??'',shown=value||o.placeholder||'';
  let out=rect(x,cy-h/2,w,h,{r:10,fill:C.card,stroke:focus>.5?C.accent:C.border,sw:focus>.5?2:1.5});
  if(shown)out+=text(x+14,cy+1,shown,{weight:400,fill:value?C.text:C.placeholder});
  if(o.caret)out+=rect(x+15+textWidth(value),cy-12,2,24,{fill:C.text});
  return out;
}
/* 分段按钮（.set-seg）：pos 为当前选中项（可以是小数，表示滑块正在移动） */
function segLayout(x,labels){
  let cursorX=x+4;
  const items=labels.map(label=>{const w=textWidth(label)+30,item={x:cursorX,w,cx:cursorX+w/2};cursorX+=w;return item});
  return {x,w:cursorX+4-x,items};
}
function segmented(x,cy,labels,pos,o={}){
  const layout=segLayout(x,labels),{items}=layout;
  const i0=Math.floor(clamp(pos,0,items.length-1)),i1=Math.min(items.length-1,i0+1),u=clamp(pos,0,items.length-1)-i0;
  const knobX=lerp(items[i0].x,items[i1].x,u),knobW=lerp(items[i0].w,items[i1].w,u);
  const active=Math.round(pos);
  return rect(x,cy-22,layout.w,44,{r:12,fill:C.g1,stroke:C.border,sw:1.5,o:o.o})
    +rect(knobX,cy-18+1.5,knobW,36,{r:9,fill:C.accent,o:.08*(o.o??1)})+rect(knobX,cy-18,knobW,36,{r:9,fill:C.card,o:o.o})
    +items.map((item,i)=>text(item.cx,cy+1,labels[i],{anchor:'middle',fill:i===active?C.accentStrong:C.muted,weight:i===active?700:400,o:o.o})).join('');
}
/* 开关（.set-switch）：p 为 0（关）到 1（开）的过渡 */
function toggle(x,y,p,o={}){
  const w=o.w??50,h=o.h??30;
  return rect(x,y-h/2,w,h,{r:h/2,fill:p>=.5?(o.on??C.green):C.g3,o:o.o})
    +circle(x+h/2+(w-h)*ease.inOut(clamp(p)),y,h/2-4,{fill:C.card,o:o.o});
}
/* 进度条：ratio 为 null 时画来回滑动的不确定进度 */
function progress(x,y,w,h,ratio,t,o={}){
  let out=rect(x,y,w,h,{r:h/2,fill:C.g2});
  if(ratio==null){
    const u=(t%1200)/1200,seg32=w*.32,start=lerp(-seg32,w,ease.inOut(u));
    const a=Math.max(0,start),b=Math.min(w,start+seg32);
    if(b>a)out+=rect(x+a,y,b-a,h,{r:h/2,fill:o.fill??C.accent});
  }else out+=rect(x,y,Math.max(ratio>0?h:0,w*clamp(ratio)),h,{r:h/2,fill:o.fill??C.accent});
  return out;
}
/* 弹出菜单（.set-menu）：items = [{icon,label,danger}]；x 为右端，y 为顶边 */
function menuLayout(x,y,items){
  const w=Math.max(...items.map(item=>textWidth(item.label)))+76,h=items.length*46+12;
  return {x:x-w,y,w,h,rows:items.map((item,i)=>({cx:x-w/2,cy:y+6+23+i*46}))};
}
function menu(x,y,items,o={}){
  const layout=menuLayout(x,y,items);
  let out=rect(layout.x,layout.y+4,layout.w,layout.h,{r:12,fill:C.accent,o:.1})+rect(layout.x,layout.y,layout.w,layout.h,{r:12,fill:C.card,stroke:C.border,sw:1.5});
  items.forEach((item,i)=>{
    const cy=layout.rows[i].cy,color=item.danger?C.red:C.text;
    if(o.hover===i)out+=rect(layout.x+6,cy-21,layout.w-12,42,{r:9,fill:item.danger?C.redSoft:C.g1});
    out+=glyph(item.icon,layout.x+30,cy,20,item.danger?C.red:C.muted)+text(layout.x+52,cy+1,item.label,{fill:color,weight:400});
  });
  return g(out,{o:o.o});
}
/* 对话框（.modal-box）：标题栏下方 3px 色条表示类型；lines 为正文各行；buttons 靠右 */
const DIALOG_TONES=Object.freeze({info:C.blue,success:C.green,warning:C.orange,error:C.red});
const DIALOG_ICONS=Object.freeze({info:'info',success:'circleCheck',warning:'triangleAlert',error:'circleAlert'});
function dialogLayout(o){
  const w=o.w??460,body=(o.lines?.length??0)*32+(o.extra??0);
  const h=62+22+body+(o.buttons?.length?64:10);
  const x=320-w/2,y=Math.max(18,180-h/2);
  let right=x+w-22;
  const buttons=(o.buttons||[]).slice().reverse().map(spec=>{
    const bw=buttonWidth(spec.label,{icon:spec.icon});
    const item={cx:right-bw/2,cy:y+h-34,w:bw};right-=bw+12;return item;
  }).reverse();
  return {x,y,w,h,bodyY:y+62+22,buttons};
}
function dialog(o){
  const layout=dialogLayout(o),{x,y,w,h}=layout;
  const tone=DIALOG_TONES[o.tone]??C.blue;
  let box=rect(x,y+6,w,h,{r:16,fill:C.accentStrong,o:.16})+rect(x,y,w,h,{r:16,fill:C.card})
    +glyph(DIALOG_ICONS[o.tone]??'info',x+34,y+31,22,tone)+text(x+54,y+32,o.title,{size:22,fill:C.accent})
    +line(x,y+62,x+w,y+62,{stroke:C.border,sw:1.5,cap:'butt'})+rect(x,y+61,w,3,{fill:tone});
  (o.lines||[]).forEach((value,i)=>{box+=text(x+24,layout.bodyY+12+i*32,value,{weight:400,fill:o.ink?.[i]??C.muted})});
  (o.buttons||[]).forEach((spec,i)=>{
    const at=layout.buttons[i];
    box+=button(at.cx,at.cy,spec.label,{kind:spec.kind??'out',icon:spec.icon,press:spec.press,disabled:spec.disabled,h:40});
  });
  if(o.inner)box+=o.inner(layout);
  const appear=o.appear??1;
  return rect(0,0,640,360,{fill:C.accentStrong,o:.34*appear*(o.o??1)})
    +g(box,{x:320*(1-lerp(.96,1,ease.out(appear))),y:(y+h/2)*(1-lerp(.96,1,ease.out(appear)))+(1-ease.out(appear))*10,
      s:lerp(.96,1,ease.out(appear)),o:Math.min(o.o??1,appear)});
}
/* 底部提示条：从卡片底部滑上来（与参考演示的“已保存”提示一致） */
const TOAST_TONES=Object.freeze({ok:C.green,info:C.accentStrong,warn:C.warn,error:C.red});
function toast(t,o){
  const p=show(t,o.at,o.until??Infinity,320);
  if(p<=0)return '';
  const y=PANEL.y+PANEL.h-30;
  return g(rect(PANEL.left,y-22,PANEL.right-PANEL.left,44,{r:12,fill:TOAST_TONES[o.tone]??C.green})
    +(o.icon?glyph(o.icon,PANEL.left+26,y,22,C.card,{sw:2.4}):'')+text(PANEL.left+(o.icon?46:20),y+1,o.text,{fill:C.card}),
  {y:(1-ease.out(p))*16,o:p});
}

/* ── 鼠标指针 ── */
/* 指针形状：尖端在 (0,0)，下方一层浅影 */
const ARROW='M0 0L0 27L7.2 20.6L12.4 31.6L17.6 29.2L12.6 18.6L21.6 18.4Z';
function cursor(x,y,o={}){
  return g(path(ARROW,{fill:C.accentStrong,o:.22})+'',{x:x+1.6,y:y+2.4,s:1-.12*(o.press??0),o:o.o})
    +g(path(ARROW,{fill:C.card,stroke:C.accentStrong,sw:2}),{x,y,s:1-.12*(o.press??0),o:o.o});
}
function ripple(x,y,p){
  if(p<=0||p>=1)return '';
  return circle(x,y,lerp(6,26,ease.out(p)),{stroke:C.accent,sw:3,o:(1-p)*.7});
}
/* 演示用的指针：keys = [[时间, x, y], …] 依次移动（段内先快后稳），clicks 为按下的时刻。
   第一帧之前 200ms 淡入，until 之后淡出。 */
function pointer(t,keys,clicks=[],o={}){
  const start=keys[0][0],until=o.until??Infinity;
  if(t<start-200||t>=until)return '';
  const [x,y]=track(t,keys,ease.cursor);
  const ripples=clicks.map(at=>{const [rx,ry]=track(at,keys,ease.cursor);return ripple(rx,ry,seg(t,at,520))}).join('');
  return ripples+cursor(x,y,{press:pressAny(t,clicks),o:Math.min(seg(t,start-200,200),until===Infinity?1:1-seg(t,until-220,220))});
}

/* ── 说明标注 ── */
/* 深色标注（与页面术语提示同一外观）：尖角指向锚点 (x,y)；side 为标注在锚点的哪一侧，
   dx/dy 让标注相对尖角偏移；text 可用 \n 分两行。at 弹出，until 前 200ms 淡出。 */
const NOTE=Object.freeze({pad:14,line:28,tail:9,gap:4,edge:8});
function noteBox(spec){
  const lines=String(spec.text).split('\n');
  const w=Math.max(...lines.map(value=>textWidth(value)))+NOTE.pad*2,h=lines.length*NOTE.line+14;
  const side=spec.side??'top',reach=NOTE.tail+NOTE.gap;
  let left,top;
  if(side==='top'||side==='bottom'){
    left=spec.x-w/2+(spec.dx??0);
    top=side==='top'?spec.y-reach-h:spec.y+reach;
  }else{
    top=spec.y-h/2+(spec.dy??0);
    left=side==='left'?spec.x-reach-w:spec.x+reach;
  }
  left=clamp(left,NOTE.edge,640-NOTE.edge-w);
  top=clamp(top,NOTE.edge,360-NOTE.edge-h);
  return {lines,w,h,left,top,side};
}
function callout(t,spec,index=0){
  const until=spec.until??Infinity;
  const appear=seg(t,spec.at,260);
  if(appear<=0||t>=until)return '';
  const fade=until===Infinity?1:1-seg(t,until-200,200);
  const box=noteBox(spec);
  // 相对锚点绘制，缩放从尖角弹出；尖角根部始终落在标注边框以内
  const L=box.left-spec.x,T=box.top-spec.y,{w,h}=box,gap=NOTE.gap,half=8;
  let tip;
  if(box.side==='top'){const cx=clamp(0,L+16,L+w-16);tip=`M${f(cx-half)} ${f(T+h-.5)}L${f(cx)} ${f(-gap)}L${f(cx+half)} ${f(T+h-.5)}Z`}
  else if(box.side==='bottom'){const cx=clamp(0,L+16,L+w-16);tip=`M${f(cx-half)} ${f(T+.5)}L${f(cx)} ${f(gap)}L${f(cx+half)} ${f(T+.5)}Z`}
  else if(box.side==='left'){const cy=clamp(0,T+14,T+h-14);tip=`M${f(L+w-.5)} ${f(cy-half)}L${f(-gap)} ${f(cy)}L${f(L+w-.5)} ${f(cy+half)}Z`}
  else{const cy=clamp(0,T+14,T+h-14);tip=`M${f(L+.5)} ${f(cy-half)}L${f(gap)} ${f(cy)}L${f(L+.5)} ${f(cy+half)}Z`}
  let inner=rect(L,T+3,w,h,{r:10,fill:C.accentStrong,o:.18})+rect(L,T,w,h,{r:10,fill:C.accentStrong})+path(tip,{fill:C.accentStrong});
  box.lines.forEach((value,i)=>{inner+=text(L+NOTE.pad,T+7+NOTE.line/2+i*NOTE.line,value,{fill:C.card})});
  return g(inner,{x:spec.x,y:spec.y,s:lerp(.86,1,ease.back(appear)),o:Math.min(1,appear*1.8)*fade,cls:'xp-callout',callout:index});
}
/* 一段动画的全部标注：按时间排好的常量表 */
const notes=(t,list)=>list.map((spec,index)=>callout(t,spec,index)).join('');

settingsPage.explainerStage=Object.freeze({C,clamp,f,seg,lerp,bezier,ease,show,track,arc,pressAt,pressAny,typed,caretOn,esc,
  rect,circle,line,path,text,g,textWidth,glyph,spin,card,PANEL,panel,chip,chipWidth,pill,pillWidth,saved,button,buttonWidth,iconButton,
  field,segLayout,segmented,toggle,progress,menuLayout,menu,dialogLayout,dialog,toast,cursor,ripple,pointer,NOTE,noteBox,callout,notes});
})();
