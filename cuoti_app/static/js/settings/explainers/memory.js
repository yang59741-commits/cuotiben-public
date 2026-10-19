(function initMemoryExplainer(){
'use strict';
/* AI 记忆（16 秒）界面演示：分析完成、试卷发布各记一笔事件 → 打开画像管理，看“稳定”和“近期”
   → 画像只作组卷的软参考 → 关掉学习开关、清空一类 → 再打开画像，确认准确后亮起金边 */
const S=window.Cuoti.settings.explainerStage;
const {C,f,seg,ease,show,g,rect,line,path,text,glyph,panel,PANEL,iconButton,button,segmented,toggle,menu,dialog,dialogLayout,
  toast,textWidth,pointer,notes,pressAt}=S;

const BOX_Y=74,BOX_W=182,BOX_H=158;
const BOXES=Object.freeze([
  {key:'free',label:'自由组卷',x:36},
  {key:'mistakes',label:'错题学习',x:229},
  {key:'past',label:'真题组卷',x:422}
]);
const FOOT_Y=268,OPEN_X=138;

/* 时间轴：两条通知各记一笔 → 点“查看和管理画像” → 关抽屉、关学习开关、⋯ → 清空 → 再打开、确认准确 */
const EVENT_AT=Object.freeze({mistakes:550,free:1600});
const OPEN_1=3200,CLOSE_AT=8700,PAUSE_AT=9450,MORE_AT=10950,CLEAR_ITEM=11550,CLEAR_OK=12250,OPEN_2=14050,CONFIRM_AT=14750;
const CLEARED=CLEAR_OK+100;

function counts(t,key){
  if(key==='free')return t>=CLEARED?{profiles:0,events:0}:{profiles:3,events:t>=EVENT_AT.free?9:8};
  if(key==='mistakes')return {profiles:5,events:t>=EVENT_AT.mistakes?13:12};
  return {profiles:2,events:6};
}

/* 暂停的记忆盒（.mem-space.is-paused）是 135° 斜纹：逐条裁进盒子的八边形近似轮廓，不依赖 pattern/clipPath 的 id */
function clipHalf(poly,a,b,c){
  const out=[];
  poly.forEach((cur,i)=>{
    const prev=poly[(i+poly.length-1)%poly.length];
    const dc=a*cur[0]+b*cur[1]-c,dp=a*prev[0]+b*prev[1]-c;
    const cross=()=>{const k=dp/(dp-dc);return [prev[0]+(cur[0]-prev[0])*k,prev[1]+(cur[1]-prev[1])*k]};
    if(dc<=0){if(dp>0)out.push(cross());out.push(cur)}
    else if(dp<=0)out.push(cross());
  });
  return out;
}
function stripes(x,y,w,h,o){
  const k=7,x0=x+1,y0=y+1,x1=x+w-1,y1=y+h-1,period=40;
  const outline=[[x0+k,y0],[x1-k,y0],[x1,y0+k],[x1,y1-k],[x1-k,y1],[x0+k,y1],[x0,y1-k],[x0,y0+k]];
  let d='';
  for(let u=Math.floor((x0+y0)/period)*period;u<x1+y1;u+=period){
    const band=clipHalf(clipHalf(outline,-1,-1,-u),1,1,u+period/2);
    if(band.length>2)d+=`M${band.map(([px,py])=>`${f(px)} ${f(py)}`).join('L')}Z`;
  }
  return d?path(d,{fill:C.g2,o:.6*o}):'';
}

/* 新记的一笔：数字旁边浮起“+1” */
function plusOne(t,x,y,at){
  if(at==null)return '';
  const p=seg(t,at,600);
  if(p<=0||p>=1)return '';
  return text(x,y-14*ease.out(p),'+1',{weight:800,fill:C.accent,o:Math.min(1,p*4)*(1-ease.in(p))});
}

const menuOpen=t=>Math.min(seg(t,MORE_AT+40,160),1-seg(t,CLEAR_ITEM+40,140));

/* ── 记忆盒（.mem-space）：名称 + ⋯、画像条数、事件数、学习开关 ── */
function spaceBox(t,spec){
  const {x,key}=spec,data=counts(t,key);
  const paused=key==='mistakes'?ease.inOut(seg(t,PAUSE_AT+20,200)):0;
  const flash=EVENT_AT[key]==null?0:Math.sin(Math.PI*seg(t,EVENT_AT[key],600));
  const meta=paused>=.5?'不记新事件':data.events?`${data.events} 个事件`:'还没有记录';
  const figure=String(data.profiles);
  let out=rect(x,BOX_Y,BOX_W,BOX_H,{r:12,fill:C.soft,stroke:C.border,sw:1.5});
  if(paused>0)out+=stripes(x,BOX_Y,BOX_W,BOX_H,paused);
  if(flash>0)out+=rect(x,BOX_Y,BOX_W,BOX_H,{r:12,stroke:C.accentLight,sw:2.5,o:flash});
  out+=text(x+14,BOX_Y+24,spec.label,{weight:800})
    +iconButton(x+BOX_W-24,BOX_Y+23,'more',key==='free'?{size:32,press:pressAt(t,MORE_AT),active:menuOpen(t)>0}:{size:32})
    +text(x+14,BOX_Y+64,figure,{size:32,weight:800,fill:C.accentStrong})
    +text(x+20+textWidth(figure,32),BOX_Y+68,'条画像',{weight:600,fill:C.muted})
    +text(x+14,BOX_Y+98,meta,{weight:400,fill:C.muted})
    +plusOne(t,x+22+textWidth(meta),BOX_Y+98,EVENT_AT[key])
    +toggle(x+14,BOX_Y+134,1-paused)
    +text(x+74,BOX_Y+135,paused>=.5?'已暂停':'学习中',{fill:paused>=.5?C.muted:C.green});
  // 开关保存后右侧短暂出现对勾（.set-saved）
  if(key==='mistakes')out+=glyph('check',x+BOX_W-24,BOX_Y+134,20,C.green,{sw:2.6,o:show(t,PAUSE_AT+220,PAUSE_AT+1300,200)});
  return out;
}

/* 卡片底部：入口按钮 + 合计；清空后换成结果提示 */
function foot(t){
  const press=Math.max(pressAt(t,OPEN_1),pressAt(t,OPEN_2));
  const total=BOXES.reduce((sum,spec)=>{const c=counts(t,spec.key);return {events:sum.events+c.events,profiles:sum.profiles+c.profiles}},{events:0,profiles:0});
  const cleared=seg(t,CLEARED+60,240);
  return button(OPEN_X,FOOT_Y,'查看和管理画像',{kind:'out',icon:'chevronRight',iconAfter:true,h:40,press})
    +text(252,FOOT_Y+1,`共 ${total.events} 个事件 · ${total.profiles} 条画像`,{weight:400,fill:C.muted,o:1-cleared})
    +g(glyph('check',262,FOOT_Y,20,C.green,{sw:2.6})+text(278,FOOT_Y+1,'已清空“自由组卷”记忆',{fill:C.green}),{o:cleared});
}

const TOASTS=Object.freeze([
  {at:200,until:1250,text:'错题分析完成',icon:'circleCheck',tone:'ok'},
  {at:1300,until:2650,text:'试卷已发布',icon:'examPaper',tone:'info'}
]);

/* ── ⋯ 菜单与清空确认 ── */
const MENU_ITEMS=Object.freeze([{icon:'layoutGrid',label:'查看画像'},{icon:'refresh',label:'重建摘要'},{icon:'trash',label:'清空这一类',danger:true}]);
function moreMenu(t){
  const open=menuOpen(t);
  if(open<=0)return '';
  return menu(BOXES[0].x+BOX_W-8,BOX_Y+45,MENU_ITEMS,{hover:t>=CLEAR_ITEM-130?2:null,o:open});
}
const DIALOG=Object.freeze({title:'清空 AI 记忆',tone:'error',
  lines:['将永久清空“自由组卷”的 9 个事件','和 3 条画像，无法恢复。','错题、试卷和另外两类记忆不受影响。'],
  ink:[C.text,C.text,C.muted]});
const CLEAR_BUTTON=dialogLayout({...DIALOG,buttons:[{label:'取消'},{label:'清空'}]}).buttons[1];
function clearDialog(t){
  const appear=seg(t,CLEAR_ITEM+60,240),close=seg(t,CLEAR_OK+60,200);
  if(appear<=0||close>=1)return '';
  return dialog({...DIALOG,buttons:[{label:'取消',kind:'out'},{label:'清空',kind:'d',press:pressAt(t,CLEAR_OK)}],
    appear:ease.out(appear),o:1-close});
}

/* ── 画像管理抽屉（.set-drawer）：从右侧滑入，盖住卡片 ── */
const DRAWER_X=212,DRAWER_W=416,LIST_Y=168,ENTRY_H=88;
const SCOPES=Object.freeze(['自由组卷','错题学习','真题组卷']);
const HINT='画像会自动形成并供组卷参考';
const ENTRIES=Object.freeze({
  free:[{type:'明确偏好',value:'少出纯计算题',status:'stable',evidence:3},{type:'薄弱点',value:'换元积分常漏回代',status:'recent',evidence:2}],
  mistakes:[{type:'薄弱点',value:'定积分换限出错',status:'stable',evidence:4},{type:'掌握信号',value:'极限运算较熟练',status:'recent',evidence:2}]
});
const STATUS=Object.freeze({
  recent:{label:'近期',fill:C.blueSoft,color:C.blue},
  stable:{label:'稳定',fill:C.accentSoft,color:C.accent},
  confirmed:{label:'已确认',fill:C.greenSoft,color:C.green}
});
const drawerOpen=t=>Math.max(0,ease.out(seg(t,OPEN_1+50,300))-ease.in(seg(t,CLOSE_AT+50,250)))+ease.out(seg(t,OPEN_2+50,300));

/* 一条画像（.mem-entry）：类型 + 内容；状态、证据数，右侧“确认准确”和 ⋯。确认后左侧金边、内容右移 */
function entry(X,item,index,confirm,press){
  const top=LIST_Y+index*ENTRY_H,left=X+24+8*ease.out(confirm);
  const tagW=textWidth(item.type)+20;
  const status=confirm>=.5?STATUS.confirmed:STATUS[item.status],pillW=textWidth(status.label)+24;
  return (confirm>0?rect(X+14,top+12,4,ENTRY_H-24,{r:2,fill:C.gold,o:confirm}):'')
    +rect(left,top+13,tagW,30,{r:7,fill:C.g1})+text(left+10,top+29,item.type,{fill:C.muted})
    +text(left+tagW+10,top+29,item.value)
    +rect(left,top+47,pillW,30,{r:15,fill:status.fill})+text(left+12,top+63,status.label,{fill:status.color})
    +text(left+pillW+12,top+63,`证据 ${item.evidence} 条`,{weight:400,fill:C.muted})
    +button(X+294,top+62,'确认准确',{kind:'out',h:34,press,o:1-confirm})
    +iconButton(X+376,top+62,'more',{size:32})
    +(index?'':line(X+16,top+ENTRY_H,X+DRAWER_W-16,top+ENTRY_H,{stroke:C.g2,sw:1.5,cap:'butt'}));
}

function drawer(t){
  const open=drawerOpen(t);
  if(open<=0)return '';
  const X=DRAWER_X+(1-open)*(DRAWER_W+28);
  const later=t>=OPEN_2;
  const confirm=later?ease.out(seg(t,CONFIRM_AT+100,300)):0;
  const done=later?seg(t,CONFIRM_AT+150,250):0;
  // 第三段：提示语“供组卷参考”像荧光笔一样划出来
  const mark=later?0:ease.inOut(seg(t,5900,450))*(1-seg(t,8100,200));
  return rect(PANEL.x,PANEL.y,PANEL.w,PANEL.h,{r:16,fill:C.accentStrong,o:.3*open})
    +rect(X-5,12,DRAWER_W,336,{r:16,fill:C.accentStrong,o:.12})+rect(X,12,DRAWER_W,336,{r:16,fill:C.card,stroke:C.border,sw:1.5})
    +text(X+24,46,'画像管理',{size:22,weight:800})+iconButton(X+DRAWER_W-36,46,'x',{size:36,press:pressAt(t,CLOSE_AT)})
    +line(X,72,X+DRAWER_W,72,{stroke:C.border,sw:1.5,cap:'butt'})
    +segmented(X+24,104,SCOPES,later?1:0)
    +(mark>0?rect(X+16,131,(textWidth(HINT)+16)*mark,30,{r:6,fill:C.warnSoft,stroke:C.warnLine,sw:1}):'')
    +text(X+24,147,HINT,{weight:400,fill:mark>.5?C.text:C.muted,o:1-done})
    +g(glyph('check',X+34,146,20,C.green,{sw:2.6})+text(X+50,147,'画像已确认，之后会优先参考',{fill:C.green}),{o:done})
    +line(X,LIST_Y,X+DRAWER_W,LIST_Y,{stroke:C.g2,sw:1.5,cap:'butt'})
    +ENTRIES[later?'mistakes':'free'].map((item,i)=>entry(X,item,i,i===0?confirm:0,i===0&&later?pressAt(t,CONFIRM_AT):0)).join('');
}

const NOTES=Object.freeze([
  {at:650,until:2650,x:320,y:76,side:'top',text:'自动记一笔，不用确认'},
  {at:3750,until:5450,x:226,y:230,side:'left',text:'攒够 3 次、跨 2 天\n→ 成为稳定画像'},
  {at:4350,until:5450,x:226,y:318,side:'left',text:'近期：还在积累'},
  {at:5900,until:8250,x:226,y:146,side:'left',text:'组卷时只作软参考\n本次填写的要求优先'},
  {at:9700,until:10800,x:285,y:122,side:'top',text:'已有的照常参考'},
  {at:12550,until:13650,x:127,y:184,side:'bottom',text:'只清空这一类'},
  {at:14900,x:224,y:212,side:'left',text:'已确认：优先参考'},
  {at:15100,x:588,y:246,side:'bottom',text:'不准就纠正或隐藏'}
]);
const CURSOR=Object.freeze([
  [0,566,258],[2750,566,258],[3100,170,272],[3350,170,272],[3750,400,238],
  [5650,400,238],[6000,512,160],[8250,512,160],[8550,592,48],[8800,592,48],
  [8950,592,48],[9300,270,210],[9550,270,210],[10450,270,210],[10800,196,99],[11000,196,99],
  [11100,196,99],[11400,120,242],[11650,120,242],[12150,CLEAR_BUTTON.cx+4,CLEAR_BUTTON.cy+2],[12350,CLEAR_BUTTON.cx+4,CLEAR_BUTTON.cy+2],
  [12500,CLEAR_BUTTON.cx+4,CLEAR_BUTTON.cy+2],[12850,470,300],[13600,470,300],[13900,170,272],[14150,170,272],
  [14300,170,272],[14650,500,234],[14850,500,234],[15000,500,234],[15350,540,170]
]);

function draw(t){
  return panel('brain','AI 记忆')+iconButton(584,46,'refresh')
    +BOXES.map(spec=>spaceBox(t,spec)).join('')+foot(t)
    +TOASTS.map(spec=>toast(t,spec)).join('')
    +moreMenu(t)+drawer(t)+clearDialog(t)
    +notes(t,NOTES)
    +pointer(t,CURSOR,[OPEN_1,CLOSE_AT,PAUSE_AT,MORE_AT,CLEAR_ITEM,CLEAR_OK,OPEN_2,CONFIRM_AT]);
}

window.Cuoti.settings.explainer.defineScene('memory',draw);
})();
