(function initStatsFormat(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.stats=window.Cuoti.stats||{};

  const STATS_EVENT_LABELS=Object.freeze({pass:'本次答对',assisted:'提示后完成',again:'需要再练',manual_master:'手动标记掌握',manual_reopen:'手动重新练习'});
  const STATS_GRANULARITY_LABELS=Object.freeze({day:'按日',week:'按周',month:'按月'});
  const WEEKDAYS='日一二三四五六';
  // Review stages follow the fixed 3-5-10-15 schedule (services/review_schedule.py).
  const STAGES=Object.freeze([
    Object.freeze({name:'第1轮',rule:'新题或答错后 3 天',color:'var(--stage-1)'}),
    Object.freeze({name:'第2轮',rule:'答对后隔 5 天',color:'var(--stage-2)'}),
    Object.freeze({name:'第3轮',rule:'答对后隔 10 天',color:'var(--stage-3)'}),
    Object.freeze({name:'第4轮',rule:'答对后隔 15 天',color:'var(--stage-4)'}),
    Object.freeze({name:'已掌握',rule:'第4轮答对后',color:'var(--chart-mastered)'})
  ]);
  const OUTCOMES=Object.freeze([
    Object.freeze({key:'pass',label:STATS_EVENT_LABELS.pass,color:'var(--chart-pass)'}),
    Object.freeze({key:'assisted',label:STATS_EVENT_LABELS.assisted,color:'var(--chart-hint)'}),
    Object.freeze({key:'again',label:STATS_EVENT_LABELS.again,color:'var(--chart-again)'})
  ]);

  const count=value=>Math.max(0,safeInteger(value));
  function statsPercent(value){return value===null||value===undefined||!Number.isFinite(Number(value))?'样本不足':`${Math.round(Number(value)*100)}%`}
  const pad=number=>String(number).padStart(2,'0');
  function dayKey(date){return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`}
  function parseDay(text){
    const match=/^(\d{4})-(\d{2})-(\d{2})/.exec(String(text||''));
    return match?new Date(Number(match[1]),Number(match[2])-1,Number(match[3])):null;
  }
  function addDays(day,amount){const date=parseDay(day);date.setDate(date.getDate()+amount);return dayKey(date)}
  function diffDays(from,to){return Math.round((parseDay(to)-parseDay(from))/86400000)}
  function bucketOf(day,granularity){
    if(granularity==='month')return day.slice(0,7);
    if(granularity==='week'){const date=parseDay(day);date.setDate(date.getDate()-((date.getDay()+6)%7));return dayKey(date)}
    return day;
  }
  function formatStatsDate(value){
    const match=/^(\d{4})-(\d{2})-(\d{2})/.exec(String(value||''));
    return match?`${match[1]}年${Number(match[2])}月${Number(match[3])}日`:'';
  }
  function dayLabel(day){const date=parseDay(day);return date?`${date.getMonth()+1}月${date.getDate()}日`:''}
  function dayWeekLabel(day){const date=parseDay(day);return date?`${dayLabel(day)} 周${WEEKDAYS[date.getDay()]}`:''}
  function shortDay(day){const date=parseDay(day);return date?`${date.getMonth()+1}/${date.getDate()}`:''}
  function periodLabel(period,granularity){
    const text=String(period||'');
    if(granularity==='month')return /^\d{4}-\d{2}/.test(text)?`${Number(text.slice(0,4))}年${Number(text.slice(5,7))}月`:text;
    if(granularity==='week')return `${dayLabel(text)}起一周`;
    return dayWeekLabel(text);
  }
  function formatInsightRange(range){
    if(!range||typeof range!=='object'||Array.isArray(range))return '统计范围见上方筛选';
    const parts=[];
    if(range.date_from&&range.date_to)parts.push(`${formatStatsDate(range.date_from)}至${formatStatsDate(range.date_to)}`);
    else if(range.as_of)parts.push(`截至${formatStatsDate(range.as_of)}`);
    if(range.recent_from)parts.push(`近期自${formatStatsDate(range.recent_from)}起`);
    if(range.subject)parts.push(`科目：${String(range.subject)}`);
    return parts.filter(Boolean).join(' · ')||'统计范围见上方筛选';
  }
  function statsPresetRange(preset,today=new Date()){
    const end=new Date(today.getFullYear(),today.getMonth(),today.getDate());
    const days={'7d':7,'30d':30,'90d':90}[preset];
    if(days){const start=new Date(end);start.setDate(end.getDate()-(days-1));return [start,end]}
    if(preset==='term'){
      // Autumn term runs from September to January, spring term from February.
      const month=end.getMonth();
      const start=month>=8?new Date(end.getFullYear(),8,1):
        month===0?new Date(end.getFullYear()-1,8,1):new Date(end.getFullYear(),1,1);
      return [start,end];
    }
    return null;
  }
  function heatmapLevel(value,maximum){
    const reviews=count(value);
    if(!reviews)return 0;
    return Math.min(4,Math.ceil(reviews/Math.max(1,safeInteger(maximum,1))*4));
  }
  function niceMax(value){
    if(value<=4)return 4;
    const step=Math.pow(10,Math.floor(Math.log10(value))),ratio=value/step;
    return (ratio<=2?2:ratio<=2.5?2.5:ratio<=5?5:10)*step;
  }
  // Streaks count consecutive active days inside the selected range only.
  function studyStreaks(heatmap,scope){
    const active=new Set((Array.isArray(heatmap)?heatmap:[]).filter(row=>count(row.reviews)).map(row=>String(row.day)));
    let end=scope.date_to,current=0,label='连续学习';
    if(!active.has(end)){end=addDays(end,-1);label='到昨天连续'}
    while(end>=scope.date_from&&active.has(end)){current+=1;end=addDays(end,-1)}
    let longest=0,run=0;
    const days=diffDays(scope.date_from,scope.date_to)+1;
    for(let index=0;index<days;index+=1){run=active.has(addDays(scope.date_from,index))?run+1:0;longest=Math.max(longest,run)}
    return Object.freeze({current,label,longest,active:active.size,days});
  }
  // The API only returns periods with events; charts need every period in the range.
  function denseTrend(rows,scope){
    const byPeriod=new Map((Array.isArray(rows)?rows:[]).map(row=>[String(row.period),row]));
    const granularity=scope.granularity,result=[];
    let previous='';
    for(let day=scope.date_from;day<=scope.date_to;day=addDays(day,1)){
      const period=bucketOf(day,granularity);
      if(period===previous)continue;
      previous=period;
      result.push(byPeriod.get(period)||{period,reviews:0,pass:0,assisted:0,again:0,first_mastered:0,manual_changes:0,estimated:false});
    }
    return result;
  }
  // Sparklines show at most `limit` points; longer series are summed into equal chunks.
  function chunkTrend(rows,limit=14){
    const list=Array.isArray(rows)?rows:[],size=Math.max(1,Math.ceil(list.length/limit)),chunks=[];
    list.forEach((row,index)=>{
      const slot=Math.floor(index/size);
      chunks[slot]=chunks[slot]||{reviews:0,pass:0,again:0};
      chunks[slot].reviews+=count(row.reviews);chunks[slot].pass+=count(row.pass);chunks[slot].again+=count(row.again);
    });
    return chunks;
  }
  function masteryDelta(snapshots){
    const usable=(Array.isArray(snapshots)?snapshots:[]).filter(row=>count(row.total_count)>0&&parseDay(row.snapshot_day));
    if(usable.length<2)return null;
    const rate=row=>count(row.mastered_count)/count(row.total_count);
    const first=usable[0],last=usable[usable.length-1];
    return Object.freeze({from:String(first.snapshot_day),points:Math.round((rate(last)-rate(first))*100),usable});
  }

  window.Cuoti.stats.format=Object.freeze({
    STATS_EVENT_LABELS,STATS_GRANULARITY_LABELS,STAGES,OUTCOMES,count,statsPercent,dayKey,parseDay,addDays,diffDays,bucketOf,
    formatStatsDate,dayLabel,dayWeekLabel,shortDay,periodLabel,formatInsightRange,statsPresetRange,heatmapLevel,niceMax,
    studyStreaks,denseTrend,chunkTrend,masteryDelta
  });
})();
