(function initQuestionBankExplainer(){
'use strict';
/* 普通题库索引（13 秒）界面演示：状态“可用”，组卷直接查这本目录 → 点刷新发现“待重建”，旧索引照常能用
   → 点“后台重建”，检查通过才换上新索引 → 重建完接力语义索引；再重建一次，没有变化就不重复提交 */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,ease,line,text,glyph,panel,pill,iconButton,button,progress,textWidth,pointer,notes,pressAt}=S;

const REFRESH_AT=3300,STALE_AT=3800,REBUILD_1=6250,RUN_1=6550,DONE_1=7900,REBUILD_2=10650,RUN_2=10900,DONE_2=11500;
const BUTTON=Object.freeze({cx:108,cy:186}),MSG_X=196;

/* 卡片在各时刻的状态；“提交中”沿用上一条提示（真实页面在提交结束后才重画提示） */
const STALE_LINES=['题库文件改过了，旧索引照常可用'],RUN_LINES=['在后台进行，可以离开本页'];
const DONE_LINES_1=['重建完成，组卷已在使用新索引','已接着提交语义索引更新'];
const DONE_LINES_2=['重建完成，组卷已在使用新索引','语义索引已对应这份题库，无需重复更新'];
const BEFORE={count:'1,286',time:'10-18 21:40'},AFTER={count:'1,302',time:'10-19 09:52'};
const PHASES=Object.freeze([
  {from:0,tone:'ok',state:'可用',button:'idle',...BEFORE},
  {from:STALE_AT,tone:'warn',state:'待重建',button:'stale',...BEFORE,lines:STALE_LINES,since:STALE_AT},
  {from:REBUILD_1,tone:'warn',state:'待重建',button:'submit',...BEFORE,lines:STALE_LINES,since:STALE_AT},
  {from:RUN_1,tone:'busy',state:'重建中',button:'run',...BEFORE,lines:RUN_LINES,since:RUN_1,run:true},
  {from:DONE_1,tone:'ok',state:'可用',button:'idle',...AFTER,lines:DONE_LINES_1,since:DONE_1,ok:true},
  {from:REBUILD_2,tone:'ok',state:'可用',button:'submit',...AFTER,lines:DONE_LINES_1,since:DONE_1,ok:true},
  {from:RUN_2,tone:'busy',state:'重建中',button:'run',...AFTER,lines:RUN_LINES,since:RUN_2,run:true},
  {from:DONE_2,tone:'ok',state:'可用',button:'idle',...AFTER,time:'10-19 09:53',lines:DONE_LINES_2,since:DONE_2,ok:true}
]);
const phaseAt=t=>PHASES.reduce((found,item)=>(t>=item.from?item:found),PHASES[0]);

function header(t,p){
  const pop=1+.1*Math.sin(Math.PI*Math.max(seg(t,STALE_AT,300),seg(t,DONE_1,300)*(t<REBUILD_2?1:0)));
  return pill(556,46,p.state,p.tone,{t,s:pop})
    +iconButton(584,46,'refresh',{press:pressAt(t,REFRESH_AT),spin:ease.inOut(seg(t,REFRESH_AT,STALE_AT-REFRESH_AT))*360,
      active:t>=REFRESH_AT-300&&t<STALE_AT+200});
}

/* 三个数字（.bank-figure）：题目数、题源可用、最近一次重建；换上新索引时闪一下 */
function figure(x,value,unit,label,o){
  return text(x,102,value,{size:28,weight:800,fill:C.accentStrong,o})
    +(unit?text(x+textWidth(value,28)+4,106,unit,{weight:600,fill:C.muted,o}):'')
    +text(x,134,label,{weight:400,fill:C.muted});
}
function figures(t,p){
  const o=1-.8*Math.max(Math.sin(Math.PI*seg(t,DONE_1-160,320)),Math.sin(Math.PI*seg(t,DONE_2-160,320)));
  return figure(36,p.count,'道','题目',o)+figure(166,'3/3','','题源可用')+figure(280,p.time,'','最近一次重建',o);
}

function rebuildButton(t,p){
  if(p.button==='submit'||p.button==='run')
    return button(BUTTON.cx,BUTTON.cy,p.button==='run'?'重建中…':'提交中…',{kind:'out',icon:'loader',busy:true,t,disabled:true});
  return button(BUTTON.cx,BUTTON.cy,'后台重建',{kind:p.button==='stale'?'p':'out',icon:'refresh',
    press:Math.max(pressAt(t,REBUILD_1),pressAt(t,REBUILD_2))});
}

function message(t,p){
  if(!p.lines)return '';
  const o=seg(t,p.since,220),top=p.lines.length>1?BUTTON.cy-11:BUTTON.cy+1;
  return p.lines.map((value,i)=>text(MSG_X,top+i*24,value,{weight:400,fill:p.ok?C.green:C.muted,o})).join('');
}

/* 折叠的“详细信息”：这段演示不展开，只画出卡片底部的样子 */
const details=()=>line(36,242,604,242,{stroke:C.g2,sw:1.5,cap:'butt'})
  +text(38,266,'详细信息',{fill:C.accent})+glyph('chevronDown',594,266,20,C.accent);

const NOTES=Object.freeze([
  {at:400,until:2650,x:460,y:112,side:'right',text:'题库的目录\n组卷时直接查'},
  {at:4000,until:5650,x:508,y:64,side:'bottom',dx:36,text:'TeX 文件改过了'},
  {at:4500,until:5650,x:426,y:199,side:'bottom',text:'只是不含最新内容'},
  {at:6700,until:7850,x:320,y:228,side:'bottom',text:'检查通过才替换旧的'},
  {at:8000,until:9100,x:460,y:112,side:'right',text:'新目录已换上'},
  {at:9300,until:10400,x:306,y:211,side:'bottom',text:'开了自动更新才接力'},
  {at:11650,until:12950,x:366,y:211,side:'bottom',text:'没变化不重复提交'}
]);
const CURSOR=Object.freeze([
  [0,520,300],[2850,520,300],[3200,586,50],[3400,586,50],[3750,600,150],
  [5850,600,150],[6150,112,190],[6350,112,190],[6700,440,300],
  [10250,440,300],[10550,112,190],[10750,112,190],[11100,470,300]
]);

function draw(t){
  const p=phaseAt(t);
  return panel('files','普通题库索引')+header(t,p)+figures(t,p)+rebuildButton(t,p)+message(t,p)
    +(p.run?progress(36,218,568,8,null,t):'')+details()
    +notes(t,NOTES)
    +pointer(t,CURSOR,[REFRESH_AT,REBUILD_1,REBUILD_2]);
}

window.Cuoti.settings.explainer.defineScene('question-bank',draw);
})();
