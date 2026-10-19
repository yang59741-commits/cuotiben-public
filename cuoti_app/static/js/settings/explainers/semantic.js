(function initSemanticExplainer(){
'use strict';
/* 语义索引（16 秒）界面演示：悬停“材料卡”看术语提示、覆盖率 → 组卷找题示意：没写这个词也按题意找到
   → 组卷时怎么用：仅观察 / 正式参与 → 立即更新：暂停、继续，覆盖率涨到 100% → ⋯ 完整重建要先确认 */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,ease,show,g,rect,line,text,glyph,panel,PANEL,card,chip,pill,iconButton,button,buttonWidth,field,segLayout,segmented,
  toggle,progress,menu,menuLayout,dialog,dialogLayout,textWidth,typed,caretOn,pointer,notes,pressAt}=S;

/* 时间轴 */
const FIND_IN=3300,FIELD_AT=3700,TYPE_AT=3800,FIND_OUT=6100;
const MODE_AT=8050;
const UPDATE_AT=10350,RUN_1=10600,PAUSE_AT=11400,PAUSED=11450,RESUME_AT=12650,RUN_2=12700,FRESH=13300;
const MORE_AT=13950,REBUILD_ITEM=14400,CANCEL_AT=15700;

/* 索引数字：625 / 753 道已有材料卡，待更新 128 道（过期 96 + 缺失 32），排除 18 */
const VALID=625,STALE=96,MISSING=32,EXCLUDED=18,PENDING=STALE+MISSING,ELIGIBLE=VALID+PENDING;
function runAt(t){
  if(t<RUN_1)return {phase:'pending',done:0};
  if(t<PAUSED)return {phase:'run',done:Math.floor(41*seg(t,RUN_1,PAUSE_AT-RUN_1))};
  if(t<RUN_2)return {phase:'paused',done:41};
  if(t<FRESH)return {phase:'run',done:41+Math.floor((PENDING-41)*seg(t,RUN_2,FRESH-RUN_2))};
  return {phase:'fresh',done:PENDING};
}
function counts(done){
  return {valid:VALID+done,stale:Math.max(0,STALE-done),missing:MISSING-Math.max(0,done-STALE),excluded:EXCLUDED};
}
const percent=done=>Math.floor(Math.min(1,(VALID+done)/ELIGIBLE)*100);

function header(t,run){
  const spec=run.phase==='pending'?['待更新 128 道','warn']:run.phase==='run'?[`更新中 ${Math.floor(run.done/PENDING*100)}%`,'busy']
    :run.phase==='paused'?['已暂停','warn']:['已是最新','ok'];
  return pill(556,46,spec[0],spec[1],{t})+iconButton(584,46,'refresh');
}

/* ── 覆盖率（.sem-coverage）：大数字 + 说明（“材料卡”是术语，悬停有提示）、分段条、图例 ── */
const TOP_Y=100,BAR_Y=126,LEGEND_Y=160;
const LEAD=Math.round(36+textWidth('83',36)+26+textWidth(`${VALID} / ${ELIGIBLE} 道题已有可用`));
const TERM=Object.freeze({x:LEAD,w:textWidth('材料卡')});
const PARTS=Object.freeze([['valid','可用',C.pass],['stale','过期',C.warn],['missing','缺失',C.g3],['excluded','排除',C.g2]]);
function coverage(t,run){
  const n=counts(run.done),pct=String(percent(run.done));
  const shimmer=1-.5*Math.sin(Math.PI*seg(t,FRESH-120,300));
  const textX=36+textWidth(pct,36)+26,lead=`${n.valid} / ${ELIGIBLE} 道题已有可用`,termX=textX+textWidth(lead);
  let out=text(36,TOP_Y-2,pct,{size:36,weight:800,fill:C.accentStrong,o:shimmer})+text(textX-24,TOP_Y+4,'%',{fill:C.accentStrong})
    +text(textX,TOP_Y+1,lead,{weight:400,fill:C.muted})+text(termX,TOP_Y+1,'材料卡',{weight:400,fill:C.muted})
    // 术语（.set-term）：虚线下划线，悬停时上方出现深色提示
    +line(termX,TOP_Y+14,termX+TERM.w,TOP_Y+14,{stroke:C.accentLight,sw:1.5,dash:'4 3',cap:'butt'});
  // 分段条：各段按题数分宽，段间留 2
  const total=PARTS.reduce((sum,[key])=>sum+n[key],0),shown=PARTS.filter(([key])=>n[key]>0);
  const room=568-2*(shown.length-1);
  let x=36;
  out+=rect(36,BAR_Y,568,14,{r:7,fill:C.g1});
  shown.forEach(([key,,color],i)=>{
    const w=room*n[key]/total;
    out+=rect(x,BAR_Y,w,14,{r:i===0||i===shown.length-1?7:2,fill:color});
    x+=w+2;
  });
  // 图例
  x=36;
  PARTS.forEach(([key,label,color])=>{
    const value=String(n[key]);
    out+=rect(x,LEGEND_Y-6,12,12,{r:3,fill:color})+text(x+18,LEGEND_Y+1,label,{weight:400,fill:C.muted})
      +text(x+62,LEGEND_Y+1,value,{fill:C.text});
    x+=62+textWidth(value)+22;
  });
  return out;
}

/* ── 主操作行：按状态只给一个主按钮（.sem-primary），右侧 ⋯ ── */
const ACTION_Y=196;
const UPDATE_LABEL=`立即更新 ${PENDING} 道`;
const UPDATE_W=buttonWidth(UPDATE_LABEL,{icon:'refresh'}),PAUSE_W=buttonWidth('暂停',{icon:'pause'}),RESUME_W=buttonWidth('继续',{icon:'play'});
const RUN_LABEL_X=196,PAUSE_CX=196+textWidth(`41 / ${PENDING} 道`)+12+PAUSE_W/2;
const hint=(x,value,o)=>text(x,ACTION_Y+1,value,{weight:400,fill:C.muted,o});
function actions(t,run){
  let out='';
  if(run.phase==='pending'){
    if(t>=UPDATE_AT){
      const w=buttonWidth('提交中…',{icon:'loader'});
      out+=button(36+w/2,ACTION_Y,'提交中…',{kind:'p',icon:'loader',busy:true,t})+hint(36+w+14,'只处理有变化的题，可随时暂停');
    }else{
      out+=button(36+UPDATE_W/2,ACTION_Y,UPDATE_LABEL,{kind:'p',icon:'refresh',press:pressAt(t,UPDATE_AT)})
        +hint(36+UPDATE_W+14,'只处理有变化的题，可随时暂停');
    }
  }else if(run.phase==='run'){
    out+=progress(36,ACTION_Y-4,150,8,run.done/PENDING,t)
      +text(RUN_LABEL_X,ACTION_Y+1,`${run.done} / ${PENDING} 道`,{weight:400,fill:C.muted})
      +button(PAUSE_CX,ACTION_Y,'暂停',{kind:'out',icon:'pause',press:pressAt(t,PAUSE_AT)})
      +hint(PAUSE_CX+PAUSE_W/2+14,'可以离开本页');
  }else if(run.phase==='paused'){
    out+=button(36+RESUME_W/2,ACTION_Y,'继续',{kind:'p',icon:'play',press:pressAt(t,RESUME_AT)})
      +hint(36+RESUME_W+14,`已暂停：完成 41 / ${PENDING} 道`,seg(t,PAUSED,200));
  }else{
    const w=buttonWidth('检查更新',{icon:'refresh'});
    out+=button(36+w/2-18,ACTION_Y,'检查更新',{kind:'txt',icon:'refresh',o:seg(t,FRESH,200)})
      +hint(36+w-4,'已是最新，题库重建后自动接着更新',seg(t,FRESH,200));
  }
  return out+iconButton(584,ACTION_Y,'more',{press:pressAt(t,MORE_AT),active:moreOpen(t)>0});
}

/* ── 设置区：组卷时怎么用（分段按钮即改即存）与两个开关 ── */
const MODE_Y=258,MODE_X=166;
const MODES=Object.freeze(['关闭','仅观察比较','正式参与排序']);
const MODE=segLayout(MODE_X,MODES);
const MODE_HINTS=Object.freeze(['只在后台记录“如果用了会怎样”，不影响组卷结果','按题意参与候选题排序，会改变组卷结果']);
function settings(t){
  const u=ease.inOut(seg(t,MODE_AT,220));
  return line(36,228,604,228,{stroke:C.g2,sw:1.5,cap:'butt'})
    +text(36,MODE_Y+1,'组卷时怎么用')+segmented(MODE_X,MODE_Y,MODES,1+u)
    +glyph('check',MODE_X+MODE.w+18,MODE_Y,20,C.green,{sw:2.6,o:show(t,MODE_AT+300,MODE_AT+1450,200)})
    +text(36,294,MODE_HINTS[0],{weight:400,fill:C.muted,o:1-u})+text(36,294,MODE_HINTS[1],{weight:400,fill:C.muted,o:u})
    +toggle(36,328,1,{w:44,h:26})+text(90,329,'启用语义分析')
    +toggle(316,328,1,{w:44,h:26})+text(370,329,'题库更新后自动更新');
}

/* ── 第二段：组卷找题的示意窗：字面匹配只找到 1 道，按题意又找到 2 道 ── */
const FIND=Object.freeze({x:36,y:40,w:384,h:262});
const QUERY='中值定理';
const ROWS=Object.freeze([
  {text:'用中值定理证明不等式',tag:'字面匹配',at:4250,literal:true},
  {text:'证明导数在区间内有零点',tag:'按题意',at:4700},
  {text:'用拉格朗日公式估计误差',tag:'按题意',at:4800},
  {text:'求数列的极限'}
]);
function finder(t){
  const appear=ease.out(seg(t,FIND_IN,250)),leave=seg(t,FIND_OUT,200);
  if(appear<=0||leave>=1)return '';
  const {x,y,w,h}=FIND;
  const value=typed(t,TYPE_AT,QUERY);
  let box=card(x,y,w,h,{r:14})+glyph('search',x+28,y+31,22,C.accent)+text(x+48,y+32,'组卷时找题',{size:22,weight:800,fill:C.accentStrong})
    +chip(x+w-18,y+31,'示意',{align:'end',h:30,pad:10,fill:C.g1,stroke:C.g1,color:C.muted})
    +field(x+20,y+78,w-40,{focus:t>=FIELD_AT?1:0,value,placeholder:'输入要考的知识点',caret:t>=FIELD_AT&&caretOn(t,TYPE_AT+[...QUERY].length*90)});
  ROWS.forEach((row,i)=>{
    const cy=y+124+i*36,lit=row.at==null?0:ease.out(seg(t,row.at,240));
    if(lit>0)box+=rect(x+12,cy-17,w-24,34,{r:8,fill:row.literal?C.blueSoft:C.greenSoft,o:lit});
    box+=text(x+26,cy+1,row.text,{weight:400,fill:lit>.5?C.text:C.muted});
    if(lit>0)box+=chip(x+w-22,cy,row.tag,{align:'end',h:28,pad:10,fill:C.card,stroke:row.literal?C.blue:C.greenLine,
      color:row.literal?C.blue:C.green,o:lit});
  });
  const shown=Math.min(appear,1-leave);
  return rect(PANEL.x,PANEL.y,PANEL.w,PANEL.h,{r:16,fill:C.accentStrong,o:.3*shown})
    +g(box,{y:(1-appear)*10,o:shown});
}

/* ── 第五段：⋯ 菜单里的完整重建，先确认 ── */
const MENU_ITEMS=Object.freeze([{icon:'rotateCcw',label:'完整重建…'},{icon:'archive',label:'清理历史…'}]);
const MENU_RIGHT=604,MENU_TOP=ACTION_Y+24;
const MENU_ROWS=menuLayout(MENU_RIGHT,MENU_TOP,MENU_ITEMS).rows;
const moreOpen=t=>Math.min(seg(t,MORE_AT+40,160),1-seg(t,REBUILD_ITEM+40,140));
function moreMenu(t){
  const open=moreOpen(t);
  return open>0?menu(MENU_RIGHT,MENU_TOP,MENU_ITEMS,{hover:t>=REBUILD_ITEM-150?0:null,o:open}):'';
}
const DIALOG=Object.freeze({title:'完整重建语义索引',tone:'warning',
  lines:[`将从头分析全部 ${ELIGIBLE} 道题，耗时较长。`,'旧结果会保留，新结果检查通过后才替换。','平时用“立即更新”就够了。'],ink:[C.text,C.muted,C.muted]});
const DIALOG_BUTTONS=dialogLayout({...DIALOG,buttons:[{label:'取消'},{label:'开始完整重建'}]}).buttons;
function rebuildDialog(t){
  const appear=seg(t,REBUILD_ITEM+50,240),close=seg(t,CANCEL_AT+50,180);
  if(appear<=0||close>=1)return '';
  return dialog({...DIALOG,buttons:[{label:'取消',kind:'out',press:pressAt(t,CANCEL_AT)},{label:'开始完整重建',kind:'p'}],
    appear:ease.out(appear),o:1-close});
}

const NOTES=Object.freeze([
  {at:500,until:1900,x:TERM.x+TERM.w/2,y:TOP_Y-12,side:'top',text:'AI 读题后写的简短说明\n考什么知识点、题意是什么'},
  {at:1950,until:3100,x:58,y:118,side:'bottom',dx:100,text:'覆盖率：已有材料卡的比例'},
  {at:4900,until:6050,x:412,y:FIND.y+142,side:'right',text:'没写“中值定理”\n也能按题意找到'},
  {at:6600,until:7750,x:MODE.items[1].cx,y:MODE_Y-22,side:'top',text:'仅观察：只记录，不影响组卷'},
  {at:8300,until:9600,x:MODE.items[2].cx,y:MODE_Y-22,side:'top',text:'正式参与：会改变排序'},
  {at:9950,until:11050,x:36+UPDATE_W/2,y:ACTION_Y+21,side:'bottom',text:'会产生 AI 用量'},
  {at:11550,until:12610,x:300,y:ACTION_Y+12,side:'bottom',text:'已完成的部分保留'},
  {at:14700,until:15760,x:DIALOG_BUTTONS[1].cx,y:DIALOG_BUTTONS[1].cy+20,side:'bottom',text:'最耗时、最费用量'}
]);
const CANCEL=DIALOG_BUTTONS[0];
const CURSOR=Object.freeze([
  [0,566,290],[300,566,290],[650,TERM.x+16,TOP_Y+8],[1800,TERM.x+16,TOP_Y+8],[2100,480,160],
  [3300,480,160],[3550,232,FIND.y+80],[3800,232,FIND.y+80],[4100,566,290],
  [6450,566,290],[6750,MODE.items[1].cx+24,MODE_Y+16],[7650,MODE.items[1].cx+24,MODE_Y+16],
  [7950,MODE.items[2].cx+6,MODE_Y+6],[8150,MODE.items[2].cx+6,MODE_Y+6],[8450,566,290],
  [9900,566,290],[10250,40+UPDATE_W/2,ACTION_Y+4],[10450,40+UPDATE_W/2,ACTION_Y+4],[10700,566,290],
  [11000,566,290],[11300,PAUSE_CX+4,ACTION_Y+4],[11500,PAUSE_CX+4,ACTION_Y+4],[11800,566,290],
  [12250,566,290],[12550,40+RESUME_W/2,ACTION_Y+4],[12750,40+RESUME_W/2,ACTION_Y+4],[13050,566,290],
  [13550,566,290],[13850,586,ACTION_Y+4],[14050,586,ACTION_Y+4],[14300,500,MENU_ROWS[0].cy+3],[14500,500,MENU_ROWS[0].cy+3],
  [15300,500,MENU_ROWS[0].cy+3],[15600,CANCEL.cx+4,CANCEL.cy+2],[15800,CANCEL.cx+4,CANCEL.cy+2]
]);

function draw(t){
  const run=runAt(t);
  return panel('target','语义索引')+header(t,run)+coverage(t,run)+actions(t,run)+settings(t)
    +finder(t)+moreMenu(t)+rebuildDialog(t)
    +notes(t,NOTES)
    +pointer(t,CURSOR,[FIELD_AT,MODE_AT,UPDATE_AT,PAUSE_AT,RESUME_AT,MORE_AT,REBUILD_ITEM,CANCEL_AT]);
}

window.Cuoti.settings.explainer.defineScene('semantic',draw);
})();
