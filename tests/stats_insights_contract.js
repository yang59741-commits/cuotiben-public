const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const elements=new Map();
const insights=[
  {status:'insufficient',title:'few'},
  {status:'ok',title:'healthy',message:'当前没有逾期题目',sample_size:0},
  {status:'triggered',title:'attention'},
  {status:'normal',title:'compatible'}
];
const context={
  URLSearchParams, console,
  document:{addEventListener(){},getElementById(id){
    if(!elements.has(id))elements.set(id,{value:'',innerHTML:'',classList:{toggle(){}}});
    return elements.get(id);
  }},
  window:{Cuoti:{features:{register(){}},daily:{render(){}}}},
  api:async url=>url.includes('/reminders')?{}:{insights},
  safeInteger:value=>Number(value)||0,escapeHTML:value=>String(value??''),
  problemPreviewHTML:value=>value||'',emptyStateHTML:()=>'',renderMath(){}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('cuoti_app/static/js/icons.js','utf8'),context);
vm.runInContext(fs.readFileSync('cuoti_app/static/js/stats.js','utf8'),context);
vm.runInContext('statsInitialized=true;',context);
(async()=>{
  await context.loadStats();
  const html=elements.get('stats-insights').innerHTML;
  assert.match(html, /class="insight normal"><div><strong>healthy<\/strong>.*?状态正常/);
  assert.ok(html.indexOf('attention')<html.indexOf('healthy'));
  assert.ok(html.indexOf('healthy')<html.indexOf('few'));
  assert.match(html, /class="insight normal"><div><strong>compatible/);
  assert.match(html, /class="insight insufficient"><div><strong>few/);
  assert.equal(insights[1].status,'ok');
  console.log('STATS_INSIGHTS_CONTRACT_OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
