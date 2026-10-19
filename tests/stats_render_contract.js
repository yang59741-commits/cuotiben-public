const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function stubElement(){
  return {
    innerHTML:'',className:'',textContent:'',hidden:false,dataset:{},style:{},
    classList:{toggle(){},add(){},remove(){}},
    querySelector(){return stubElement()},querySelectorAll(){return []},
    getBoundingClientRect(){return {left:0,top:0,width:0,height:0}}
  };
}
const elements=new Map();
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const context={
  URLSearchParams,console,Map,Set,Date,Math,Number,String,Array,Object,
  document:{addEventListener(){},getElementById(id){if(!elements.has(id))elements.set(id,stubElement());return elements.get(id)}},
  window:{Cuoti:{}},
  safeInteger:(value,fallback=0)=>{const parsed=Number(value);return Number.isSafeInteger(parsed)?parsed:fallback},
  escapeHTML,safeLocalURL:value=>String(value||'').startsWith('#/')?String(value):'',
  problemPreviewHTML:(value,variant)=>`<div class="${variant}">${escapeHTML(value)}</div>`,
  emptyStateHTML:(title,options)=>`<div class="empty-state" data-icon-name="${options.icon}">${escapeHTML(title)}</div>`
};
context.window.document=context.document;
vm.createContext(context);
for(const file of ['icons.js','stats/format.js','stats/charts.js','stats/render.js']){
  vm.runInContext(fs.readFileSync(`cuoti_app/static/js/${file}`,'utf8'),context);
}
const F=context.window.Cuoti.stats.format,R=context.window.Cuoti.stats.render;
const html=id=>elements.get(id).innerHTML;

// Formatting helpers.
assert.equal(F.heatmapLevel(0,10),0);
assert.equal(F.heatmapLevel(1,10),1);
assert.equal(F.heatmapLevel(10,10),4);
assert.equal(F.heatmapLevel(50,10),4);
assert.deepEqual(Array.from(F.statsPresetRange('term',new Date(2026,0,15)),F.dayKey),['2025-09-01','2026-01-15']);
assert.deepEqual(Array.from(F.statsPresetRange('7d',new Date(2026,9,17)),F.dayKey),['2026-10-11','2026-10-17']);
assert.equal(F.statsPresetRange('custom'),null);
assert.equal(F.statsPercent(null),'样本不足');
assert.equal(F.statsPercent(.456),'46%');
assert.equal(F.formatInsightRange({date_from:'2026-10-01',date_to:'2026-10-17'}),'2026年10月1日至2026年10月17日');

const dayRows=F.denseTrend([{period:'2026-10-03',reviews:4,pass:3,again:1}],{date_from:'2026-10-01',date_to:'2026-10-05',granularity:'day'});
assert.deepEqual(Array.from(dayRows,row=>row.period),['2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05']);
assert.equal(dayRows[2].reviews,4);
assert.equal(dayRows[0].reviews,0);
const weekRows=F.denseTrend([],{date_from:'2026-10-01',date_to:'2026-10-20',granularity:'week'});
assert.deepEqual(Array.from(weekRows,row=>row.period),['2026-09-28','2026-10-05','2026-10-12','2026-10-19']);
const monthRows=F.denseTrend([],{date_from:'2025-11-20',date_to:'2026-02-03',granularity:'month'});
assert.deepEqual(Array.from(monthRows,row=>row.period),['2025-11','2025-12','2026-01','2026-02']);

const streak=F.studyStreaks([{day:'2026-10-14',reviews:2},{day:'2026-10-15',reviews:1},{day:'2026-10-16',reviews:3},{day:'2026-10-10',reviews:1}],{date_from:'2026-10-08',date_to:'2026-10-17'});
assert.equal(streak.label,'到昨天连续');
assert.equal(streak.current,3);
assert.equal(streak.longest,3);
assert.equal(streak.active,4);
assert.equal(streak.days,10);

assert.equal(F.masteryDelta([{snapshot_day:'2026-10-01',total_count:10,mastered_count:2}]),null);
assert.equal(F.masteryDelta([{snapshot_day:'2026-10-01',total_count:10,mastered_count:2},{snapshot_day:'2026-10-09',total_count:10,mastered_count:5}]).points,30);

// Insights: triggered rules first, normal and low-sample rules fold into one line each; input is not mutated.
const insights=[
  {rule:'stale_subjects',status:'insufficient',title:'few',message:'',sample_size:0},
  {rule:'overdue_reviews',status:'ok',title:'healthy',message:'当前没有逾期题目',sample_size:0},
  {rule:'weak_chapters',status:'triggered',title:'attention',message:'1 个章节掌握率低于 60%',sample_size:12,metric:{threshold:.6},range:{as_of:'2026-10-17'}},
  {rule:'again_rate_rising',status:'normal',title:'compatible',message:'',sample_size:30,metric:{prior_rate:.2,recent_rate:.25}}
];
const chapterRates=[
  {chapter_id:1,chapter:'<数列>',subject_id:7,subject:'数学',total:12,mastered:3,mastery_rate:.25},
  {chapter_id:2,chapter:'导数',subject_id:7,subject:'数学',total:4,mastered:4,mastery_rate:1}
];
R.insights({insights,chapter_rates:chapterRates},{subjectId:'7'});
const insightHTML=html('stats-insights');
assert.ok(insightHTML.indexOf('attention')<insightHTML.indexOf('healthy'));
assert.ok(insightHTML.indexOf('healthy')<insightHTML.indexOf('few'));
assert.match(insightHTML,/class="insight triggered">.*?<strong>attention<\/strong><div class="insight-message">/);
assert.match(insightHTML,/class="insight-calm normal">.*?healthy、compatible：状态正常（再次答错 20% → 25%）/);
assert.match(insightHTML,/class="insight-calm insufficient">.*?few：样本不足，暂不判断/);
assert.match(insightHTML,/data-stats-focus-chapter="1" data-stats-focus-subject="7">&lt;数列&gt; 25%/);
assert.equal(insights[1].status,'ok');

// Chapter progress: escaped names, weak marks, ordered by what is left.
R.chapters({chapter_rates:chapterRates,insights});
const chapterHTML=html('chapter-chart');
assert.ok(chapterHTML.includes('&lt;数列&gt;'));
assert.ok(!chapterHTML.includes('<数列>'));
assert.ok(chapterHTML.indexOf('&lt;数列&gt;')<chapterHTML.indexOf('导数'));
assert.equal((chapterHTML.match(/class="chapter-weak"/g)||[]).length,1);
assert.equal(elements.get('chapter-chart-meta').textContent,'2 个章节 · 1 个低于 60%');

// Review journey: scoped review button when due, the bear only once the queue is clear, no reset loop arrow.
const base={
  scope:{date_from:'2026-10-01',date_to:'2026-10-17',days:17,granularity:'day'},
  summary:{total:20,mastered:5,current_mastery_rate:.25,assisted_count:2},
  review_cycle:{stage_distribution:{0:6,1:4,2:3,3:2},reset_mistakes:3,normal_mastered:1},
  daily_snapshots:[],learning_actions:[{title:'<练>',message:'m',href:'#/review?mode=spaced'},{title:'薄弱',message:'m',href:'#/review?mode=weak'}]
};
R.hero(base,{due_count:4,overdue_count:1},{subjectId:'7',chapterId:'1'});
const dueHTML=html('stats-hero');
assert.match(dueHTML,/data-action="open-spaced-review" data-subject-id="7" data-chapter-id="1"/);
assert.ok(dueHTML.includes('其中逾期 1 道'));
assert.ok(dueHTML.includes('本期 <b>3</b> 道答错后回到第1轮'));
assert.ok(!dueHTML.includes('stats-bear'));
assert.ok(!dueHTML.includes('&lt;练&gt;'),'spaced actions are covered by the main button');
assert.ok(dueHTML.includes('href="#/review?mode=weak"'));
assert.equal((dueHTML.match(/class="stats-station(?: is-goal)?"/g)||[]).length,5);
R.hero(base,{due_count:0,overdue_count:0},{});
const clearHTML=html('stats-hero');
assert.ok(clearHTML.includes('class="stats-bear"'));
assert.ok(clearHTML.includes('今日间隔复习已完成'));
assert.ok(!clearHTML.includes('data-action="open-spaced-review"'));

console.log('STATS_RENDER_CONTRACT_OK');
