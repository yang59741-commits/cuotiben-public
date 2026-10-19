(function initHealthExplainer(){
'use strict';
/* 运行状态（13 秒）界面演示：五个部件逐个检查，只查本机 → 生成 PDF 报错后点“重新诊断”，PDF 编译亮黄并写明原因
   → 装好 TeX 再诊断一次，恢复正常 → 全绿只说明本机正常；翻到 AI 服务点“测试连接”才真正连一次 AI */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,lerp,ease,g,rect,circle,text,glyph,panel,pill,iconButton,button,buttonWidth,toast,pointer,notes,pressAt}=S;

const SCAN_1=150,RESULT_1=1250,CHECK_2=3800,RESULT_2=4500,CHECK_3=7300,RESULT_3=8100;
const SCROLL_AT=10450,TEST_AT=11300,TESTED=11900;

/* ── 五个部件（.health-tile）：检查时依次亮起（与页面的 healthScan 一致，每块晚 90ms） ── */
const TILES=Object.freeze([
  {label:'数据库',icon:'database'},{label:'后台任务',icon:'activity'},{label:'PDF 编译',icon:'fileText'},
  {label:'题库',icon:'files'},{label:'AI 配置',icon:'sparkles'}
]);
const TILE_W=(568-4*8)/5,TILE_Y=74,TILE_H=76;
const tileX=i=>36+i*(TILE_W+8);
const PDF_X=tileX(2)+TILE_W/2,AI_X=tileX(4)+TILE_W/2;

function healthAt(t){
  if(t<RESULT_1)return {checked:false,warn:false,clock:''};
  if(t<RESULT_2)return {checked:true,warn:false,clock:'09:40'};
  if(t<RESULT_3)return {checked:true,warn:true,clock:'09:46'};
  return {checked:true,warn:false,clock:'09:52'};
}
const scanFrom=t=>(t>=SCAN_1&&t<RESULT_1?SCAN_1:t>=CHECK_2&&t<RESULT_2?CHECK_2+50:t>=CHECK_3&&t<RESULT_3?CHECK_3+50:null);

function tiles(t,h){
  const from=scanFrom(t);
  return TILES.map((tile,i)=>{
    const x=tileX(i),warn=h.warn&&i===2;
    let o=1,ring=0;
    if(from!=null){const p=seg(t,from+i*90,900);o=p<.6?lerp(.35,1,p/.6):1;ring=p<.6?p/.6:1-(p-.6)/.4}
    return rect(x-3,TILE_Y-3,TILE_W+6,TILE_H+6,{r:13,fill:C.accent,o:.12*ring})
      +g(rect(x,TILE_Y,TILE_W,TILE_H,{r:10,fill:warn?C.warnSoft:C.soft,stroke:warn?C.warnLine:C.g2,sw:1.5})
        +glyph(tile.icon,x+TILE_W/2,TILE_Y+26,22,C.muted)+text(x+TILE_W/2,TILE_Y+56,tile.label,{anchor:'middle'})
        +circle(x+TILE_W-12,TILE_Y+12,4.5,{fill:!h.checked?C.g4:warn?C.warn:C.green}),{o});
  }).join('');
}

function state(t,h){
  if(!h.checked)return pill(556,46,'正在检查…','busy',{t});
  const pop=1+.1*Math.sin(Math.PI*Math.max(seg(t,RESULT_1,300),seg(t,RESULT_2,300),seg(t,RESULT_3,300)));
  return h.warn?pill(556,46,'1 项警告','warn',{t,s:pop}):pill(556,46,'全部正常','ok',{t,s:pop});
}

/* ── 问题说明（.health-issue）与底部按钮：出现问题时按钮先下移，再淡入说明；恢复时反过来 ── */
const footShift=t=>ease.inOut(seg(t,RESULT_2,250))-ease.inOut(seg(t,RESULT_3+150,250));
function issue(t){
  const o=seg(t,RESULT_2+150,250)*(1-seg(t,RESULT_3,200));
  if(o<=0)return '';
  return g(rect(36,164,568,72,{r:10,fill:C.warnSoft,stroke:C.warnLine,sw:1.5})+rect(37,170,4,60,{r:2,fill:C.warn})
    +glyph('triangleAlert',60,186,20,C.warn)+text(78,186,'PDF 编译：PDF 编译组件未配置或不可用')
    +text(78,214,'安装 TeX Live 或 MiKTeX 后，点“重新诊断”',{weight:400,fill:C.muted}),{o});
}
const RECHECK_W=buttonWidth('重新诊断',{icon:'refresh'});
function foot(t,h){
  const y=lerp(192,268,footShift(t));
  const busy=(t>=CHECK_2&&t<RESULT_2)||(t>=CHECK_3&&t<RESULT_3);
  return (busy?button(36+RECHECK_W/2,y,'诊断中…',{kind:'out',icon:'loader',busy:true,t,disabled:true})
    :button(36+RECHECK_W/2,y,'重新诊断',{kind:'out',icon:'refresh',press:Math.max(pressAt(t,CHECK_2),pressAt(t,CHECK_3))}))
    +(h.clock?text(52+RECHECK_W,y+1,`检查于 ${h.clock}`,{weight:400,fill:C.muted}):'');
}

/* ── 第四段：往上翻到 AI 服务卡（只画到它底部的模型与“测试连接”） ── */
const TEST_W=buttonWidth('测试连接',{icon:'activity'});
function slot(x,label,icon,color,model){
  return rect(x,104,278,104,{r:12,fill:C.soft,stroke:C.border,sw:1.5})+rect(x+10,104,258,3,{r:1.5,fill:color,o:.7})
    +glyph(icon,x+24,132,20,color)+text(x+42,133,label,{weight:800})
    +rect(x+14,154,250,40,{r:10,fill:C.card})+text(x+28,175,model,{weight:400});
}
function aiCard(t){
  const testing=t>=TEST_AT&&t<TESTED;
  return panel('sparkles','AI 服务')+pill(604,46,'已就绪','ok')
    +text(36,86,'模型',{fill:C.muted})
    +slot(36,'快速模型','zap',C.green,'deepseek-chat')+slot(326,'高质量模型','sparkle',C.orange,'deepseek-reasoner')
    +(testing?button(36+TEST_W/2,290,'测试中…',{kind:'out',icon:'loader',busy:true,t,disabled:true})
      :button(36+TEST_W/2,290,'测试连接',{kind:'out',icon:'activity',press:pressAt(t,TEST_AT)}))
    +(testing?text(52+TEST_W,291,'正在发一条很短的请求…',{weight:400,fill:C.muted}):'')
    +(t>=TESTED?text(52+TEST_W,291,'连接正常 · deepseek-chat 用时 0.8 秒',{fill:C.green,o:seg(t,TESTED,200)}):'')
    +iconButton(584,290,'more');
}

const NOTES=Object.freeze([
  {at:1400,until:2900,x:320,y:TILE_Y,side:'top',text:'只查本机，不联系 AI'},
  {at:4700,until:5900,x:PDF_X,y:TILE_Y,side:'top',dx:-20,text:'警告：部分功能可能用不了'},
  {at:4950,until:6050,x:480,y:236,side:'bottom',text:'写明原因和处理办法'},
  {at:6200,until:7300,x:36+RECHECK_W,y:268,side:'right',text:'装好 TeX 后再点一次'},
  {at:8200,until:9300,x:PDF_X,y:TILE_Y,side:'top',text:'PDF 编译已恢复'},
  {at:9350,until:10450,x:AI_X,y:TILE_Y+TILE_H,side:'bottom',text:'本机正常 ≠ 连得上 AI'},
  {at:12050,x:260,y:272,side:'top',text:'这里才真正连一次 AI'}
]);
const CURSOR=Object.freeze([
  [0,566,250],[3350,566,250],[3700,40+RECHECK_W/2,196],[3900,40+RECHECK_W/2,196],[4200,600,300],
  [6850,600,300],[7150,40+RECHECK_W/2,272],[7450,40+RECHECK_W/2,272],[7800,600,300],
  [10900,600,300],[11200,40+TEST_W/2,294],[11400,40+TEST_W/2,294],[11750,560,240]
]);

function draw(t){
  const scroll=ease.inOut(seg(t,SCROLL_AT,550));
  const h=healthAt(t);
  const health=panel('shieldCheck','运行状态')+state(t,h)+tiles(t,h)+issue(t)+foot(t,h);
  return (scroll<1?g(health,{y:372*scroll}):'')+(scroll>0?g(aiCard(t),{y:-372*(1-scroll)}):'')
    +toast(t,{at:3050,until:4300,text:'生成 PDF 失败：找不到 XeLaTeX',icon:'circleAlert',tone:'error'})
    +notes(t,NOTES)
    +pointer(t,CURSOR,[CHECK_2,CHECK_3,TEST_AT]);
}

window.Cuoti.settings.explainer.defineScene('health',draw);
})();
