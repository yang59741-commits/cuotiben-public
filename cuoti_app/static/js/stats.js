let statsInitialized=false;
let statsRequestRevision=0;
let statsChapterRequestRevision=0;
let statsChapterRequestController=null;
function statsPercent(value){return value===null||value===undefined?'样本不足':`${Math.round(Number(value)*100)}%`}
function statsRingHTML(rate){
  const value=rate===null||rate===undefined?NaN:Number(rate);
  if(!Number.isFinite(value))return icon('check');
  const circumference=2*Math.PI*16;
  const filled=Math.max(0,Math.min(1,value))*circumference;
  return `<svg class="stat-ring" viewBox="0 0 40 40" aria-hidden="true" focusable="false"><circle class="stat-ring-track" cx="20" cy="20" r="16"/><circle class="stat-ring-value" cx="20" cy="20" r="16" stroke-dasharray="${filled.toFixed(2)} ${circumference.toFixed(2)}" transform="rotate(-90 20 20)"/></svg>${icon('check','stat-ring-icon')}`;
}
const STATS_INSIGHT_STATUS=Object.freeze({
  triggered:['circleAlert','需要关注'],
  normal:['circleCheck','状态正常'],
  insufficient:['circleDashed','样本不足']
});
function statsInsightStatusHTML(status){
  const [iconName,label]=Object.prototype.hasOwnProperty.call(STATS_INSIGHT_STATUS,status)?STATS_INSIGHT_STATUS[status]:STATS_INSIGHT_STATUS.insufficient;
  return `<span class="insight-status">${icon(iconName)}${label}</span>`;
}
function statsDateValue(value){return window.localDateInputValue?window.localDateInputValue(value):value.toISOString().slice(0,10)}
function heatmapLevel(value,maximum){
  const count=Math.max(0,safeInteger(value));
  if(!count)return 0;
  return Math.min(4,Math.ceil(count/Math.max(1,safeInteger(maximum,1))*4));
}
function statsEmptyHTML(title,compact=false,iconName='chart'){return emptyStateHTML(title,{icon:iconName,chart:true,compact})}
const STATS_EVENT_LABELS={pass:'本次掌握',assisted:'提示后完成',again:'需要再练',manual_master:'手动标记掌握',manual_reopen:'手动重新练习'};
const STATS_GRANULARITY_LABELS={day:'按日',week:'按周',month:'按月'};
function formatStatsDate(value){
  const text=String(value||'');
  const match=/^(\d{4})-(\d{2})-(\d{2})/.exec(text);
  return match?`${match[1]}年${Number(match[2])}月${Number(match[3])}日`:'';
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
function setStatsInlineStatus(message,type=''){
  const status=document.getElementById('stats-inline-status');
  if(!status)return;
  status.textContent=message||'';status.className='stats-inline-status'+(type?' '+type:'');status.hidden=!message;
}

async function initStatsFilters(){
  if(statsInitialized)return;
  statsInitialized=true;
  const end=new Date(),start=new Date();start.setDate(end.getDate()-29);
  document.getElementById('stats-date-from').value=statsDateValue(start);
  document.getElementById('stats-date-to').value=statsDateValue(end);
  const subject=document.getElementById('stats-subject');
  try{
    const subjects=await api('/api/subjects');
    subjects.forEach(item=>subject.add(new Option(String(item.name||''),String(safeInteger(item.id,'')))));
    syncCustomSelect(subject);
  }catch(error){subject.disabled=true;syncCustomSelect(subject);setStatsInlineStatus('科目筛选暂不可用，仍可查看全部统计。','error')}
  subject.addEventListener('change',async()=>{
    const chapter=document.getElementById('stats-chapter');
    const sid=subject.value;
    const revision=++statsChapterRequestRevision;
    statsChapterRequestController?.abort();
    chapter.replaceChildren(new Option(sid?'正在读取章节...':'请先选择科目',''));
    chapter.disabled=true;
    chapter.toggleAttribute('aria-busy',Boolean(sid));
    syncCustomSelect(chapter);
    if(!sid){setStatsInlineStatus('');return}
    statsChapterRequestController=new AbortController();
    try{
      const chapters=await api(
        '/api/chapters?'+new URLSearchParams({subject_id:sid}).toString(),
        {signal:statsChapterRequestController.signal}
      );
      if(revision!==statsChapterRequestRevision||subject.value!==sid)return;
      if(!Array.isArray(chapters)||!chapters.length){
        chapter.replaceChildren(new Option('该科目暂无可选章节',''));
        syncCustomSelect(chapter);
        setStatsInlineStatus('');
        return;
      }
      chapter.replaceChildren(new Option('全部章节',''));
      chapters.forEach(item=>chapter.add(new Option(String(item.name||''),String(safeInteger(item.id,'')))));
      chapter.disabled=false;syncCustomSelect(chapter);setStatsInlineStatus('');
    }catch(error){
      if(error?.name==='AbortError'||revision!==statsChapterRequestRevision)return;
      chapter.replaceChildren(new Option('章节筛选暂不可用',''));
      syncCustomSelect(chapter);
      setStatsInlineStatus('章节筛选暂不可用，当前统计内容仍保留。','error');
    }finally{
      if(revision===statsChapterRequestRevision)chapter.removeAttribute('aria-busy');
    }
  });
  document.getElementById('stats-apply').addEventListener('click',loadStats);
}

async function loadStats(){
  await initStatsFilters();
  const revision=++statsRequestRevision;
  const params=new URLSearchParams({date_from:document.getElementById('stats-date-from').value,date_to:document.getElementById('stats-date-to').value});
  const subject=document.getElementById('stats-subject').value,chapter=document.getElementById('stats-chapter').value;
  if(subject)params.set('subject_id',subject);if(chapter)params.set('chapter_id',chapter);
  const reminderParams=new URLSearchParams();
  if(subject)reminderParams.set('subject_id',subject);
  if(chapter)reminderParams.set('chapter_id',chapter);
  let d,reminder;
  try{
    [d,reminder]=await Promise.all([
      api('/api/v2/stats?'+params),
      api('/api/v2/stats/reminders'+(reminderParams.size?'?'+reminderParams:''))
    ]);
    if(revision!==statsRequestRevision)return;
  }catch(error){
    if(revision===statsRequestRevision){
      setStatsInlineStatus('统计读取失败，请稍后重试。','error');
      const errorBox=document.getElementById('stats-load-error');
      if(errorBox){errorBox.hidden=false;errorBox.querySelector('span').textContent='当前统计暂时无法更新，已保留上次显示内容。'}
    }
    return;
  }
  setStatsInlineStatus('');
  const errorBox=document.getElementById('stats-load-error');if(errorBox)errorBox.hidden=true;
  const todayReview=Math.max(0,safeInteger(reminder.due_count));
  void window.Cuoti.reviewBadge?.refresh?.();
  const s=d.summary||{},exam=d.exam_attempts||{},complete=d.completeness||{},samples=complete.samples||{},cycle=d.review_cycle||{},stages=cycle.stage_distribution||{};
  const nextActions=document.getElementById('learning-next-actions');
  if(nextActions)nextActions.innerHTML=(d.learning_actions||[]).slice(0,3).map(item=>`<div class="learning-next-row"><div><strong>${escapeHTML(item.title)}</strong><span>${escapeHTML(item.message)}</span></div><a class="btn btn-out btn-sm" href="${escapeHTML(safeLocalURL(item.href)||'#/review')}">${icon('arrowRight')}开始练习</a></div>`).join('');
  const metrics=[
    {tone:'good',visual:statsRingHTML(s.current_mastery_rate),label:'当前掌握率',value:statsPercent(s.current_mastery_rate),detail:`${safeInteger(s.mastered)} / ${safeInteger(s.total)}`},
    {tone:'accent',visual:icon('refresh'),label:'今日间隔复习',value:todayReview,detail:'按当前科目与章节的到期队列',action:true},
    {tone:'warn',visual:icon('rotateCcw'),label:'再次答错率',value:statsPercent(s.again_rate),detail:`${safeInteger(s.forgotten_mistakes)} 道需重点巩固`}
  ];
  const reviewScope=(subject?` data-subject-id="${safeInteger(subject)}"`:'')+
    (chapter?` data-chapter-id="${safeInteger(chapter)}"`:'');
  document.getElementById('stat-box').innerHTML=metrics.map(item=>{
    const empty=item.value==='样本不足';
    const go=item.action?`<span class="stat-go" aria-hidden="true">${icon('arrowRight')}</span>`:'';
    const content=`<div class="stat-icon">${item.visual}</div><div class="stat-info"><div class="stat-value-row"><div class="n${empty?' is-empty':''}">${escapeHTML(String(item.value))}</div>${go}</div><div class="l">${item.label}</div><div class="stat-detail">${escapeHTML(item.detail)}</div></div>`;
    return item.action
      ?`<button type="button" class="stat stat-action stat-tone-${item.tone}" data-action="open-spaced-review"${reviewScope} aria-label="打开间隔复习，今日待复习 ${todayReview} 道">${content}</button>`
      :`<div class="stat stat-tone-${item.tone}">${content}</div>`;
  }).join('');
  document.getElementById('stats-secondary').innerHTML=`<span><strong>${safeInteger(s.total)}</strong> 当前题目</span><span><strong>${statsPercent(exam.accuracy)}</strong> 试卷正确率</span><span><strong>${safeInteger(stages['0'])}/${safeInteger(stages['1'])}/${safeInteger(stages['2'])}/${safeInteger(stages['3'])}</strong> 第 1～4 阶段待复习</span><span><strong>${safeInteger(cycle.reset_mistakes)}</strong> 道题发生循环重置</span><span><strong>${safeInteger(cycle.normal_mastered)}</strong> 道按四阶段正常掌握</span>`;
  const granularity=STATS_GRANULARITY_LABELS[d.scope?.granularity]||'自适应';
  if(s.assisted_count)document.getElementById('stats-secondary').insertAdjacentHTML('beforeend',`<span><strong>${safeInteger(s.assisted_count)}</strong> 次提示后完成</span>`);
  document.getElementById('stats-completeness').innerHTML=`<span class="completeness-badge ${complete.range_is_complete?'complete':'estimated'}">${complete.range_is_complete?'范围内事件完整':'含不完整历史'}</span><details><summary>统计口径说明</summary><span>自 ${escapeHTML(formatStatsDate(complete.statistics_complete_since)||'可用记录')} 起完整 · 完整复习样本 ${safeInteger(samples.complete_review_events)} / 已知 ${safeInteger(samples.known_review_events)} · ${granularity}</span></details>`;
  const chapters=Array.isArray(d.chapter_rates)?d.chapter_rates:[];
  document.getElementById('chapter-chart-meta').textContent=`${chapters.length} 个章节`;
  document.getElementById('chapter-chart').innerHTML=chapters.length?`<div class="rank-chart">${chapters.map((item,index)=>`<div class="rank-row"><div class="rank-label"><span class="rank-index">${String(index+1).padStart(2,'0')}</span><span class="rank-name">${escapeHTML(item.chapter||'未归类')}</span><strong>${statsPercent(item.mastery_rate)}</strong></div><div class="rank-track"><span class="rank-fill" style="--bar-size:${Math.round(Number(item.mastery_rate||0)*100)}%"></span></div><small>${safeInteger(item.mastered)} 已掌握 / ${safeInteger(item.total)} 总题数</small></div>`).join('')}</div>`:statsEmptyHTML('当前范围没有章节题目',false,'barsHorizontal');
  const reasons=Array.isArray(d.reason_breakdown)?d.reason_breakdown:[],reasonTotal=reasons.reduce((sum,item)=>sum+safeInteger(item.count),0);
  document.getElementById('reason-chart-meta').textContent=`${reasonTotal} 次标记`;
  document.getElementById('reason-chart').innerHTML=reasons.length?`<div class="metric-list">${reasons.map(item=>`<div><span>${escapeHTML(item.reason)}</span><strong>${safeInteger(item.count)}</strong><div class="mini-track"><i style="width:${reasonTotal?Math.round(safeInteger(item.count)/reasonTotal*100):0}%"></i></div></div>`).join('')}</div>`:statsEmptyHTML('所选范围暂无错因标记',false,'pie');
  const trend=Array.isArray(d.review_trend)?d.review_trend:[],trendMax=Math.max(1,...trend.map(item=>safeInteger(item.reviews)+safeInteger(item.manual_changes)));
  document.getElementById('trend-chart-meta').textContent=`${safeInteger(s.practice_count)} 次复习 · 本次答对率 ${statsPercent(s.review_pass_rate)}`;
  document.getElementById('trend-chart').innerHTML=trend.length?`<div class="activity-legend"><span class="pass">${STATS_EVENT_LABELS.pass}</span><span class="assisted">${STATS_EVENT_LABELS.assisted}</span><span class="again">${STATS_EVENT_LABELS.again}</span><span class="manual">手动状态调整</span></div><div class="activity-chart">${trend.map(item=>`<div class="activity-row"><time>${escapeHTML(item.period)}</time><div class="activity-bars"><i class="pass" style="width:${safeInteger(item.pass)/trendMax*100}%"></i><i class="assisted" style="width:${safeInteger(item.assisted)/trendMax*100}%"></i><i class="again" style="width:${safeInteger(item.again)/trendMax*100}%"></i><i class="manual" style="width:${safeInteger(item.manual_changes)/trendMax*100}%"></i></div><strong>${safeInteger(item.reviews)}</strong></div>`).join('')}</div>`:statsEmptyHTML('所选范围暂无复习活动');
  const heat=Array.isArray(d.heatmap)?d.heatmap:[],heatMax=Math.max(1,...heat.map(item=>safeInteger(item.reviews)));
  document.getElementById('heatmap-meta').textContent=`${heat.length} 个活跃日`;
  document.getElementById('stats-heatmap').innerHTML=heat.length?`<div class="study-heatmap">${heat.map(item=>`<span role="img" data-level="${heatmapLevel(item.reviews,heatMax)}" aria-label="${escapeHTML(item.day)} 复习${safeInteger(item.reviews)}题" title="${escapeHTML(item.day)}：${safeInteger(item.reviews)} 次"><i aria-hidden="true"></i><small>${escapeHTML(item.day.slice(5))}</small></span>`).join('')}</div><div class="heatmap-legend" aria-label="学习强度从少到多"><span>少</span>${[0,1,2,3,4].map(level=>`<i data-level="${level}" aria-hidden="true"></i>`).join('')}<span>多</span></div>`:statsEmptyHTML('暂无学习日历记录',false,'calendar');
  document.getElementById('exam-stats-meta').textContent=chapter?`${safeInteger(exam.attempts_with_scoped_answers)} 次含本章作答`:`${safeInteger(exam.submitted_attempts)} 次作答`;
  const paperScore=chapter?'':`<div><strong>${statsPercent(exam.average_score_rate)}</strong><span>整卷平均得分率 · ${safeInteger(exam.scored_attempts)} 次</span></div>`;
  document.getElementById('exam-stats').innerHTML=safeInteger(exam.submitted_attempts)?`<div class="exam-metric-grid"><div><strong>${statsPercent(exam.accuracy)}</strong><span>题级正确率 · ${safeInteger(exam.answered_items)} 题</span></div>${paperScore}</div>${(exam.chapter_error_rates||[]).length?`<div class="compact-table">${exam.chapter_error_rates.slice(0,6).map(item=>`<div><span>${escapeHTML(item.chapter)}</span><strong>错 ${statsPercent(item.error_rate)}</strong><small>${safeInteger(item.incorrect)} / ${safeInteger(item.answered)}</small></div>`).join('')}</div>`:statsEmptyHTML('暂无可归类的章节表现',true,'fileText')}`:statsEmptyHTML('尚无已提交试卷，提交作答后将在这里显示表现。',true,'fileText');
  const priority={triggered:0,normal:1,insufficient:2};
  const insights=(Array.isArray(d.insights)?d.insights:[]).map(item=>({...item,status:item.status==='ok'?'normal':item.status})).sort((a,b)=>(priority[a.status]??2)-(priority[b.status]??2));
  document.getElementById('stats-insights').innerHTML=`<div class="insight-grid">${insights.map(item=>`<article class="insight ${['triggered','normal','insufficient'].includes(item.status)?item.status:'insufficient'}"><div><strong>${escapeHTML(item.title)}</strong>${statsInsightStatusHTML(item.status)}</div>${problemPreviewHTML(item.message,'insight-message')}<small>样本 ${safeInteger(item.sample_size)} · ${escapeHTML(formatInsightRange(item.range))}</small></article>`).join('')}<article class="insight reminder"><div><strong>今日间隔复习提醒</strong><span class="insight-status">${icon('bell')}${safeInteger(reminder.due_count)} 道</span></div>${problemPreviewHTML(reminder.message||'','insight-message')}<small>${reminder.suggested_reminder_time?`建议 ${escapeHTML(reminder.suggested_reminder_time)} 提醒`:'无需提醒'} · 不含后台推送</small></article></div>`;
  const recent=Array.isArray(d.recent_chapters)?d.recent_chapters:[];
  document.getElementById('recent-list').innerHTML=recent.map(item=>`<div class="recent-item"><span class="recent-dot active"></span><div class="recent-main"><div class="recent-meta"><span>${escapeHTML(item.subject||'')}</span> <b>${escapeHTML(item.chapter||'未归类')}</b></div><div class="recent-date">${escapeHTML(item.last_reviewed_at||'')} · ${safeInteger(item.review_count)} 次复习</div></div></div>`).join('')||emptyStateHTML('所选范围暂无学习章节',{icon:'history',compact:true});
  window.Cuoti.daily.render();
  renderMath(document.getElementById('p-stats'));
}

async function mountStats(){
  await window.Cuoti.daily.load();
  await loadStats();
}

function unmountStats(){
  statsRequestRevision+=1;
  statsChapterRequestRevision+=1;
  statsChapterRequestController?.abort();
}

window.Cuoti.features.register('stats',Object.freeze({
  mount:mountStats,
  unmount:unmountStats,
  refresh:mountStats
}));

