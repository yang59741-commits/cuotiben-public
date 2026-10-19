(function initRoutingExplainer(){
'use strict';
/* 模型分工（14 秒）界面演示：两条泳道 → 任务卡按推荐落位 → 点“每日分析”换到快速道、立即保存
   → 什么时候换；点“恢复推荐分工”一键还原 */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,lerp,ease,bezier,g,rect,text,glyph,panel,saved,textWidth,pointer,notes}=S;

const LANE_Y=72,LANE_H=228,LANE_W=278;
const LANES=Object.freeze({
  fast:{x:36,icon:'zap',title:'快速',sub:'回得快 · 用量少',fill:C.greenSoft,stroke:C.greenLine,color:C.green},
  quality:{x:326,icon:'sparkle',title:'高质量',sub:'想得深 · 更慢更费',fill:C.warnSoft,stroke:C.warnLine,color:C.orange}
});
const SLOT_Y=Object.freeze([174,222,270]);
const TASKS=Object.freeze([
  {key:'classification',label:'拆题与归类',icon:'listChecks',rec:'fast'},
  {key:'problem_analysis',label:'错题分析',icon:'lightbulb',rec:'quality'},
  {key:'daily_analysis',label:'每日分析',icon:'calendar',rec:'quality'},
  {key:'exam_generation',label:'智能组卷',icon:'examPaper',rec:'quality'}
]);

const DROP_AT=3600,HOVER_AT=8150,CLICK_AT=8600,MOVE_MS=480,SAVED_AT=9150;
const RESET_HOVER=12750,RESET_CLICK=12950,RESTORED_AT=13500;
const flip=bezier(.2,.8,.2,1);   // 与页面换道动画同一缓动

/* 每条道里的顺序与真实界面一致：按任务表顺序排 */
function slots(assign){
  const out={};
  for(const lane of Object.keys(LANES))TASKS.filter(task=>assign[task.key]===lane).forEach((task,slot)=>{out[task.key]={lane,slot}});
  return out;
}
const REC=Object.fromEntries(TASKS.map(task=>[task.key,task.rec]));
const AT_REC=slots(REC),AT_MOVED=slots({...REC,daily_analysis:'fast'});
/* 0 = 推荐分工，1 = “每日分析”已换到快速道 */
const moved=t=>flip(seg(t,CLICK_AT+40,MOVE_MS))-flip(seg(t,RESET_CLICK+40,MOVE_MS));

function lane(t,key,focus){
  const spec=LANES[key];
  return rect(spec.x,LANE_Y,LANE_W,LANE_H,{r:14,fill:spec.fill,stroke:spec.stroke,sw:1.5,dash:'7 5'})
    +rect(spec.x,LANE_Y,LANE_W,LANE_H,{r:14,stroke:spec.color,sw:2.5,o:focus*.55})
    +glyph(spec.icon,spec.x+26,LANE_Y+28,22,spec.color)
    +text(spec.x+46,LANE_Y+29,spec.title,{size:22})
    +text(spec.x+18,LANE_Y+60,spec.sub,{weight:400,fill:C.muted});
}

/* 任务卡（.task-chip）：图标 + 名称 + “推荐”；指针悬停时描边加深、露出换道图标 */
function taskChip(task,x,y,o){
  const labelW=textWidth(task.label),tagW=64*o.tag,swapW=28*o.hover;
  const w=14+20+8+labelW+tagW+swapW+14;
  let inner=rect(0,-18,w,40,{r:20,fill:C.accent,o:.07+.06*o.hover})
    +rect(0,-20,w,40,{r:20,fill:C.card,stroke:o.hover>.5?C.accentLight:C.border,sw:1.5})
    +glyph(task.icon,24,0,20,C.accent)+text(42,1,task.label);
  if(o.tag>0)inner+=g(rect(0,-14,56,28,{r:14,fill:C.g1})+text(28,1,'推荐',{anchor:'middle',fill:C.muted}),{x:42+labelW+8,o:o.tag});
  if(o.hover>0)inner+=glyph('repeat',42+labelW+tagW+22,0,18,C.muted,{o:o.hover});
  return g(inner,{x,y,o:o.o});
}

function chips(t){
  const u=moved(t);
  return TASKS.map((task,i)=>{
    const a=AT_REC[task.key],b=AT_MOVED[task.key];
    const drop=ease.out(seg(t,DROP_AT+i*150,320));
    if(drop<=0)return '';
    const x=lerp(LANES[a.lane].x,LANES[b.lane].x,u)+16;
    const y=lerp(SLOT_Y[a.slot],SLOT_Y[b.slot],u)-(1-drop)*16;
    const daily=task.key==='daily_analysis';
    const hover=daily?Math.min(seg(t,HOVER_AT,160),1-seg(t,CLICK_AT+220,200)):0;
    return {daily,markup:taskChip(task,x,y,{tag:daily?1-u:1,hover,o:drop})};
  }).filter(Boolean).sort((a,b)=>a.daily-b.daily).map(item=>item.markup).join('');
}

/* 卡片底部：换道后出现“恢复推荐分工”，说明文字随之右移 */
const RESET_W=166;
function foot(t){
  const shown=Math.min(seg(t,SAVED_AT+150,220),1-seg(t,RESET_CLICK+160,200));
  const hover=Math.min(seg(t,RESET_HOVER,150),1-seg(t,RESET_CLICK+160,160));
  let out='';
  if(shown>0){
    out+=g(rect(0,-18,RESET_W,36,{r:9,fill:C.accentSoft,o:hover})+glyph('rotateCcw',20,0,20,C.accent)+text(38,1,'恢复推荐分工',{fill:C.accent}),
      {x:36,y:324,o:shown});
  }
  return out+text(36+(RESET_W+12)*ease.inOut(shown),325,'只影响之后新开始的任务',{weight:400,fill:C.muted});
}

const NOTES=Object.freeze([
  {at:500,until:3400,x:110,y:142,side:'bottom',dx:24,text:'每条道用一档模型'},
  {at:1500,until:3400,x:400,y:142,side:'bottom',text:'模型在 AI 服务里选'},
  {at:4500,until:7400,x:230,y:196,side:'bottom',dx:-40,text:'推荐：按任务难度分'},
  {at:9300,until:10700,x:130,y:244,side:'bottom',dx:36,text:'立即保存，不用点保存'},
  {at:10950,until:12500,x:175,y:248,side:'bottom',text:'嫌慢、想省 → 换快速'},
  {at:11350,until:12500,x:465,y:248,side:'bottom',text:'不满意 → 换回高质量'}
]);
/* 指针：点完“每日分析”就让开，免得停在补位上来的“智能组卷”上 */
const CURSOR=Object.freeze([
  [0,588,318],[7700,588,318],[8250,452,226],[8750,452,226],[9300,560,318],[12250,560,318],[12750,118,328]
]);

function draw(t){
  const lit=(from,to)=>Math.min(seg(t,from,240),1-seg(t,to-240,240));
  return panel('route','模型分工')
    +saved(604,46,'已保存',Math.min(seg(t,SAVED_AT,200),1-seg(t,10500,300)))
    +saved(604,46,'已恢复推荐分工',seg(t,RESTORED_AT,200))
    +lane(t,'fast',Math.max(lit(500,3400),lit(10950,12500)))+lane(t,'quality',Math.max(lit(1500,3400),lit(11350,12500)))
    +chips(t)+foot(t)
    +notes(t,NOTES)
    +pointer(t,CURSOR,[CLICK_AT,RESET_CLICK]);
}

window.Cuoti.settings.explainer.defineScene('routing',draw);
})();
