(function initUsageExplainer(){
'use strict';
/* AI 使用情况（12 秒）界面演示：按任务结果看“个” → 切到“按 AI 调用”看“次”，重试让调用比任务多
   → P50/P95 用量条与截断 → 点刷新，“需要留意”指出哪类任务出了问题 */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,lerp,ease,g,rect,line,text,panel,iconButton,segmented,segLayout,pointer,notes,pressAt}=S;

const SEG_X=36,SEG_Y=196,SEG_LABELS=Object.freeze(['按任务结果','按 AI 调用']);
const SEG=segLayout(SEG_X,SEG_LABELS);
const SWITCH_AT=3650,REFRESH_AT=9750,UPDATE_AT=10350;
const ROW_Y=Object.freeze([238,278,318]);

/* 统计口径切换：0 = 按任务结果，1 = 按 AI 调用 */
const view=t=>ease.inOut(seg(t,SWITCH_AT,220));
const fresh=t=>t>=UPDATE_AT;

/* ── 顶部三格数字（.usage-kpis） ── */
function kpis(t){
  const x0=36,w=568/3,after=fresh(t);
  const shimmer=Math.sin(Math.PI*seg(t,UPDATE_AT-160,320));
  const cells=[
    {label:'任务成功率',value:after?'96.6%':'98.5%',note:after?'203 个 · 失败 7':'196 个 · 失败 3'},
    {label:'AI 调用',value:after?'438':'412',unit:'次',note:after?'重试 15 · 截断 1':'重试 6 · 截断 1'},
    {label:'需要留意',value:after?'1':'无',unit:after?'类':'',note:after?'错题分析':'都在正常范围',watch:after}
  ];
  let out=rect(x0,74,568,92,{r:12,fill:C.soft,stroke:C.g2,sw:1.5})
    +line(x0+w,74,x0+w,166,{stroke:C.g2,sw:1.5,cap:'butt'})+line(x0+2*w,74,x0+2*w,166,{stroke:C.g2,sw:1.5,cap:'butt'});
  cells.forEach((cell,i)=>{
    const x=x0+i*w+16;
    out+=text(x,96,cell.label,{fill:C.muted});
    const value=g(text(x,125,cell.value,{size:28,weight:800,fill:cell.watch?C.warnText:C.accentStrong})
      +(cell.unit?text(x+S.textWidth(cell.value,28)+4,128,cell.unit,{weight:400,fill:C.muted}):''),{o:1-.8*shimmer});
    out+=value+text(x,152,cell.note,{weight:400,fill:C.muted,o:1-.8*shimmer});
  });
  return out;
}

/* ── 列表（.usage-row） ── */
const JOBS=Object.freeze([
  {name:'错题分析',rate:.983,count:'120 个',flag:'失败 2'},
  {name:'题目录入',rate:1,count:'64 个'},
  {name:'试卷生成',rate:.917,count:'12 个',flag:'失败 1'}
]);
const CALLS=Object.freeze([
  {name:'错题分析',p50:1.2,p95:3.8,count:'186 次',flag:'重试 4',quiet:true},
  {name:'拆题与归类',p50:.6,p95:1.1,count:'152 次'},
  {name:'每日分析',p50:2.1,p95:4.9,count:'30 次',flag:'截断 1'}
]);
const WATCHED=Object.freeze({...CALLS[0],p50:1.3,p95:4.1,count:'212 次',flag:'重试 9',quiet:false,watch:true});
const MAX_P95=4.9,RANGE_X=152,RANGE_W=120;
const k=value=>`${value.toFixed(1)}k`;

function flag(x,cy,label,o={}){
  const w=S.textWidth(label)+16;
  return g(rect(x,cy-14,w,28,{r:14,fill:o.quiet?C.g1:C.warnSoft})+text(x+8,cy+1,label,{fill:o.quiet?C.muted:C.warnText,weight:o.quiet?400:700}),{s:o.s,o:o.o});
}
function jobRow(row,cy){
  const low=row.rate<.95;
  return text(44,cy+1,row.name)
    +rect(152,cy-5,200,10,{r:5,fill:C.track})+rect(152,cy-5,200*row.rate,10,{r:5,fill:low?C.again:C.mastered})
    +text(412,cy+1,`${Math.round(row.rate*100)}%`,{anchor:'end'})
    +text(492,cy+1,row.count,{anchor:'end',weight:400,fill:C.muted})
    +(row.flag?flag(504,cy,row.flag):'');
}
function callRow(t,row,cy,o={}){
  const p95=RANGE_W*row.p95/MAX_P95,p50=RANGE_W*row.p50/MAX_P95;
  return text(44,cy+1,row.name)
    +rect(RANGE_X,cy-5,RANGE_W,10,{r:5,fill:C.g1})+rect(RANGE_X,cy-5,p95,10,{r:5,fill:C.range})
    +rect(RANGE_X,cy-5,p50,10,{r:5,fill:C.accent})
    +(o.pulse>0?rect(RANGE_X+p50,cy-9,p95-p50,18,{r:9,stroke:C.accent,sw:2,o:o.pulse}):'')
    +text(282,cy+1,`${k(row.p50)} / ${k(row.p95)}`,{weight:400,fill:C.muted})
    +text(492,cy+1,row.count,{anchor:'end',weight:400,fill:C.muted})
    +(row.flag?flag(504,cy,row.flag,{quiet:row.quiet,s:o.flagPop}):'');
}
function rows(t){
  const v=view(t),after=fresh(t);
  let out='';
  ROW_Y.forEach((cy,i)=>{
    if(i)out+=line(36,cy-20,604,cy-20,{stroke:C.g2,sw:1.5,cap:'butt'});
    const watch=i===0&&after;
    if(watch)out+=rect(36,cy-19,568,38,{r:8,fill:C.warnSoft,o:seg(t,UPDATE_AT,240)});
    if(v<1)out+=g(jobRow(JOBS[i],cy),{o:1-v});
    if(v>0){
      const row=watch?WATCHED:CALLS[i];
      const pulse=i===2?Math.sin(Math.PI*seg(t,6300,700))*.9:0;
      const flagPop=i===2?1+.12*Math.sin(Math.PI*seg(t,7250,360)):watch?lerp(.6,1,ease.back(seg(t,UPDATE_AT+60,300))):1;
      out+=g(callRow(t,row,cy,{pulse,flagPop}),{o:v*(i===0?1-.8*Math.sin(Math.PI*seg(t,UPDATE_AT-160,320)):1)});
    }
  });
  return out;
}

function toolbar(t){
  const v=view(t);
  return segmented(SEG_X,SEG_Y,SEG_LABELS,v,{})
    +text(SEG_X+SEG.w+14,SEG_Y+1,'任务最终有没有拿到结果',{weight:400,fill:C.muted,o:1-v})
    +text(SEG_X+SEG.w+14,SEG_Y+1,'深色 P50 · 浅色 P95',{weight:400,fill:C.muted,o:v});
}

function header(t){
  const after=fresh(t);
  const spinning=seg(t,REFRESH_AT,UPDATE_AT-REFRESH_AT);
  return text(552,47,after?'最近 341 条记录':'最近 326 条记录',{anchor:'end',weight:400,fill:C.muted})
    +iconButton(584,46,'refresh',{press:pressAt(t,REFRESH_AT),spin:ease.inOut(spinning)*360,active:t>=REFRESH_AT-300&&t<UPDATE_AT+200});
}

const NOTES=Object.freeze([
  {at:300,until:2900,x:470,y:250,side:'bottom',dx:-30,text:'分析一道题 = 1 个任务'},
  {at:4100,until:5900,x:300,y:168,side:'bottom',dx:100,text:'所以调用比任务多'},
  {at:4700,until:5900,x:541,y:252,side:'bottom',text:'临时出错会自动重试'},
  {at:6300,until:8900,x:272,y:306,side:'top',text:'P95 偏高：少数请求很长'},
  {at:7300,until:8900,x:541,y:306,side:'top',text:'写到上限被截断'},
  {at:10500,until:11950,x:440,y:168,side:'bottom',text:'这类任务要留意'},
  {at:10900,until:11950,x:541,y:252,side:'bottom',text:'检查网络或换个模型'}
]);
const CURSOR=Object.freeze([
  [0,560,334],[3000,560,334],[3450,SEG.items[1].cx+10,SEG_Y+6],[4000,SEG.items[1].cx+10,SEG_Y+6],[4500,380,334],
  [6000,380,334],[6350,250,325],[7000,250,325],[7400,420,340],[9150,420,340],[9600,588,52]
]);

function draw(t){
  return panel('gauge','AI 使用情况')+header(t)+kpis(t)+toolbar(t)+rows(t)
    +notes(t,NOTES)
    +pointer(t,CURSOR,[SWITCH_AT,REFRESH_AT]);
}

window.Cuoti.settings.explainer.defineScene('usage',draw);
})();
