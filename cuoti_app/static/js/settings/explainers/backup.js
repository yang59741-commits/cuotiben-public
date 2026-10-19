(function initBackupExplainer(){
'use strict';
/* 数据备份（15 秒）界面演示：导出备份 → 确认导出，下载 ZIP → 什么时候导出 → 导入备份：选择方式、合并先看预览
   → 替换错题：多一道确认、输入确认文字，提交前自动创建恢复点 */
const S=window.Cuoti.settings.explainerStage;
const {C,seg,ease,g,rect,text,glyph,panel,field,dialog,dialogLayout,toast,typed,caretOn,pointer,notes,pressAt}=S;

const FILE='错题可移植备份_2026-10-19.zip';
const CONFIRM_TEXT='替换 86 道';

/* ── 两块操作（.backup-tile）：图标 + 名称 + 一句说明；悬停时变浅棕底 ── */
const TILES=Object.freeze([
  {x:36,icon:'download',title:'导出备份',note:'生成一个可移植 ZIP'},
  {x:326,icon:'upload',title:'导入备份',note:'先预览差异再写入'}
]);
const TILE_Y=84,TILE_W=278,TILE_H=88;
const tileCenter=i=>[TILES[i].x+TILE_W/2,TILE_Y+TILE_H/2];
const HOVER=Object.freeze([[[450,800]],[[6250,6600],[9800,10150]]]);
function tile(t,i){
  const spec=TILES[i];
  const hover=Math.max(0,...HOVER[i].map(([from,to])=>Math.min(seg(t,from,120),1-seg(t,to,160))));
  const press=Math.max(...(i?[pressAt(t,IMPORT_1),pressAt(t,IMPORT_2)]:[pressAt(t,EXPORT_AT)]));
  return g(rect(spec.x,TILE_Y,TILE_W,TILE_H,{r:12,fill:hover>.5?C.accentSoft:C.soft,stroke:hover>.5?C.accentLight:C.border,sw:1.5})
    +rect(spec.x+14,TILE_Y+24,40,40,{r:12,fill:C.card})+glyph(spec.icon,spec.x+34,TILE_Y+44,22,C.accent)
    +text(spec.x+68,TILE_Y+31,spec.title,{weight:800})+text(spec.x+68,TILE_Y+59,spec.note,{weight:400,fill:C.muted}),
  {x:(spec.x+TILE_W/2)*.03*press,y:(TILE_Y+TILE_H/2)*.03*press,s:1-.03*press});
}

/* ── 对话框（与页面的 showModal 同一套文案）：每个窗口 [出现, 点下关闭]；前一个关掉后下一个才出现 ── */
const EXPORT_AT=650,EXPORTED=1350;
const IMPORT_1=6450,MERGE_AT=7800,CANCEL_AT=9300;
const IMPORT_2=10000,REPLACE_AT=10700,REPLACE_OK=12000,FIELD_AT=12550,TYPE_AT=12650,SUBMIT_AT=13700;
const TYPED_AT=TYPE_AT+([...CONFIRM_TEXT].length-1)*90;
const DIALOGS=Object.freeze({
  exportConfirm:{title:'确认导出',tone:'info',lines:['将导出全部错题、复习历史和附件','为可移植 ZIP。'],
    buttons:[{label:'取消'},{label:'确认导出',kind:'s'}],open:EXPORT_AT+50,close:EXPORTED,press:[1,EXPORTED]},
  choose1:{title:'选择导入方式',tone:'info',lines:[`已选择 ${FILE}`,'两种方式都会先生成可确认预览'],
    buttons:[{label:'取消'},{label:'安全合并',kind:'s'},{label:'替换错题',kind:'d'}],open:IMPORT_1+50,close:MERGE_AT,press:[1,MERGE_AT]},
  mergePreview:{title:'确认合并预览',tone:'info',lines:['将新增 12 道、合并 3 道；','字段冲突 0 道、歧义 0 道。'],
    buttons:[{label:'取消'},{label:'确认提交',kind:'s'}],open:MERGE_AT+220,close:CANCEL_AT,press:[0,CANCEL_AT]},
  choose2:{title:'选择导入方式',tone:'info',lines:[`已选择 ${FILE}`,'两种方式都会先生成可确认预览'],
    buttons:[{label:'取消'},{label:'安全合并',kind:'s'},{label:'替换错题',kind:'d'}],open:IMPORT_2+50,close:REPLACE_AT,press:[2,REPLACE_AT]},
  replaceConfirm:{title:'确认替换错题',tone:'error',lines:['将用备份替换当前全部错题和复习历史；','提交前会自动创建恢复点。','科目、章节、试卷和设置不会改变。'],
    buttons:[{label:'取消'},{label:'确认替换',kind:'d'}],open:REPLACE_AT+220,close:REPLACE_OK,press:[1,REPLACE_OK]},
  replacePreview:{title:'确认替换预览',tone:'error',lines:['将新增 120 道、合并 0 道；',`请输入“${CONFIRM_TEXT}”确认：`],extra:56,
    buttons:[{label:'取消'},{label:'确认提交',kind:'d'}],open:REPLACE_OK+220,close:SUBMIT_AT,press:[1,SUBMIT_AT]},
  done:{title:'导入成功',tone:'success',lines:['替换完成：写入 120 道。','自动恢复点：已创建'],
    buttons:[{label:'确定',kind:'p'}],open:SUBMIT_AT+220,close:Infinity}
});
const LAYOUT=Object.fromEntries(Object.entries(DIALOGS).map(([key,spec])=>[key,dialogLayout(spec)]));
const at=(key,index)=>[LAYOUT[key].buttons[index].cx+4,LAYOUT[key].buttons[index].cy+2];

function modal(t,key){
  const spec=DIALOGS[key];
  const appear=seg(t,spec.open,240),close=seg(t,spec.close+50,160);
  if(appear<=0||close>=1)return '';
  const buttons=spec.buttons.map((button,i)=>({...button,kind:button.kind??'out',
    press:spec.press&&spec.press[0]===i?pressAt(t,spec.press[1]):0,
    disabled:key==='replacePreview'&&i===1&&t<TYPED_AT}));
  const inner=key==='replacePreview'?layout=>field(layout.x+24,layout.bodyY+90,layout.w-48,{focus:t>=FIELD_AT?1:0,
    value:typed(t,TYPE_AT,CONFIRM_TEXT),caret:t>=FIELD_AT&&caretOn(t,TYPED_AT)}):null;
  return dialog({...spec,ink:spec.lines.map((_,i)=>(i?C.muted:C.text)),buttons,inner,appear:ease.out(appear),o:1-close});
}

const NOTES=Object.freeze([
  {at:1650,until:2950,x:300,y:296,side:'top',text:'全部错题、复习历史和附件'},
  {at:3200,until:5850,x:tileCenter(0)[0],y:TILE_Y+TILE_H,side:'bottom',text:'换电脑 · 重装系统前\n做大改动之前'},
  {at:4100,until:5900,x:400,y:296,side:'top',text:'存到别的盘或 U 盘'},
  {at:6800,until:7900,x:LAYOUT.choose1.buttons[1].cx,y:LAYOUT.choose1.buttons[1].cy+20,side:'bottom',text:'冲突时默认保留本地'},
  {at:8250,until:9350,x:LAYOUT.mergePreview.buttons[1].cx,y:LAYOUT.mergePreview.buttons[1].cy+20,side:'bottom',text:'确认后才写入'},
  {at:10980,until:12050,x:LAYOUT.replaceConfirm.buttons[1].cx,y:LAYOUT.replaceConfirm.buttons[1].cy+20,side:'bottom',text:'比合并多一道确认'},
  {at:14100,x:300,y:LAYOUT.done.bodyY+44,side:'right',text:'出问题可用恢复点找回'}
]);
const CURSOR=Object.freeze([
  [0,520,300],[250,520,300],[550,...tileCenter(0)],[750,...tileCenter(0)],[1050,...at('exportConfirm',1)],[1450,...at('exportConfirm',1)],[1750,560,230],
  [6050,560,230],[6350,...tileCenter(1)],[7300,...tileCenter(1)],[7650,...at('choose1',1)],[8600,...at('choose1',1)],
  [8950,...at('mergePreview',0)],[9450,...at('mergePreview',0)],
  [9650,...at('mergePreview',0)],[9900,...tileCenter(1)],[10100,...tileCenter(1)],[10550,...at('choose2',2)],[10850,...at('choose2',2)],
  [11500,...at('choose2',2)],[11850,...at('replaceConfirm',1)],[12100,...at('replaceConfirm',1)],
  [12450,LAYOUT.replacePreview.x+210,LAYOUT.replacePreview.bodyY+94],[13250,LAYOUT.replacePreview.x+210,LAYOUT.replacePreview.bodyY+94],
  [13550,...at('replacePreview',1)],[13800,...at('replacePreview',1)],[14150,560,320]
]);

function draw(t){
  return panel('archive','数据备份')+tile(t,0)+tile(t,1)
    +toast(t,{at:1550,until:5900,text:`已下载 ${FILE}`,icon:'download',tone:'ok'})
    +Object.keys(DIALOGS).map(key=>modal(t,key)).join('')
    +notes(t,NOTES)
    +pointer(t,CURSOR,[EXPORT_AT,EXPORTED,IMPORT_1,MERGE_AT,CANCEL_AT,IMPORT_2,REPLACE_AT,REPLACE_OK,FIELD_AT,SUBMIT_AT]);
}

window.Cuoti.settings.explainer.defineScene('backup',draw);
})();
