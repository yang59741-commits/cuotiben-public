(function initStatsRender(){
  'use strict';

  const F=window.Cuoti.stats.format;
  const C=window.Cuoti.stats.charts;
  const {count,statsPercent,STAGES}=F;
  const byId=id=>document.getElementById(id);
  const pct=(part,whole)=>whole?Math.round(part/whole*100):0;
  function statsEmptyHTML(title,iconName,description=''){return emptyStateHTML(title,{icon:iconName,chart:true,description})}
  function reviewScopeAttributes(subjectId,chapterId){
    return (subjectId?` data-subject-id="${safeInteger(subjectId)}"`:'')+(chapterId?` data-chapter-id="${safeInteger(chapterId)}"`:'');
  }

  function skeleton(){
    byId('stats-hero').className='stats-hero';
    byId('stats-hero').innerHTML='<div class="stats-skeleton" style="grid-column:1/-1;height:268px"></div>';
    byId('stat-box').innerHTML='<div class="stats-skeleton" style="grid-column:1/-1;height:92px"></div>';
    byId('stats-rhythm-tools').innerHTML='';
    byId('stats-rhythm').innerHTML='<div class="stats-skeleton" style="height:240px"></div>';
    for(const id of ['chapter-chart','reason-chart','stats-insights','exam-stats','recent-list'])byId(id).innerHTML='<div class="stats-skeleton" style="height:150px"></div>';
  }

  function completeness(d){
    const complete=d.completeness||{},samples=complete.samples||{},pill=byId('stats-completeness');
    pill.hidden=false;
    pill.className='stats-pill '+(complete.range_is_complete?'complete':'estimated');
    pill.innerHTML=complete.range_is_complete?`${icon('circleCheck')}事件完整`:`${icon('info')}含估算历史`;
    const granularity=F.STATS_GRANULARITY_LABELS[d.scope?.granularity]||'自适应';
    byId('stats-completeness-pop').innerHTML=`<h3>统计口径</h3><dl><dt>完整记录起点</dt><dd>${escapeHTML(F.formatStatsDate(complete.statistics_complete_since)||'暂无')}</dd><dt>复习样本</dt><dd>完整 ${count(samples.complete_review_events)} / 已知 ${count(samples.known_review_events)}</dd><dt>时间粒度</dt><dd>${granularity}（90 天内按日，365 天内按周）</dd><dt>复习规则</dt><dd>${escapeHTML(d.formula_version||'')}</dd></dl>`;
  }

  // Review journey: the four spaced-review rounds and today's queue.
  function hero(d,reminder,{entering=false,subjectId=null,chapterId=null}={}){
    const host=byId('stats-hero'),s=d.summary||{},cycle=d.review_cycle||{},stages=cycle.stage_distribution||{};
    const counts=[0,1,2,3].map(index=>count(stages[String(index)])).concat(count(s.mastered));
    const total=counts.reduce((sum,value)=>sum+value,0);
    const delta=F.masteryDelta(d.daily_snapshots);
    let deltaHTML='',spark='';
    if(delta){
      const tone=delta.points>0?'up':delta.points<0?'down':'flat';
      deltaHTML=`<span class="stats-delta ${tone}">较${escapeHTML(F.dayLabel(delta.from))} ${delta.points>0?'+':''}${delta.points} 个百分点</span>`;
      const offsets=delta.usable.map(row=>F.diffDays(d.scope.date_from,row.snapshot_day));
      const values=Array.from({length:count(d.scope.days)||1},()=>null);
      delta.usable.forEach((row,index)=>{values[offsets[index]]=count(row.mastered_count)/count(row.total_count)});
      const gaps=offsets.slice(1).map((offset,index)=>offset-offsets[index]>1);
      const line=C.sparkline(values,{width:150,height:34,stroke:'var(--chart-mastered)',last:'var(--accent-strong)',gaps});
      if(line)spark=`<div class="stats-mastery-spark">${line}<span>掌握率走势 · ${delta.usable.length} 个快照日</span></div>`;
    }
    const rate=s.current_mastery_rate===null||s.current_mastery_rate===undefined?null:Math.round(Number(s.current_mastery_rate)*100);
    const segments=counts.map((value,index)=>value?`<button type="button" class="stats-pipe-seg" style="flex-grow:${value};background:${STAGES[index].color};--i:${index}" data-stage="${index}" aria-label="${STAGES[index].name} ${value} 道，占 ${pct(value,total)}%">${index===4&&value/total>.1?icon('check'):''}</button>`:'').join('');
    const stations=counts.map((value,index)=>`<button type="button" class="stats-station${index===4?' is-goal':''}" data-stage="${index}"><span class="stats-station-name"><i class="stats-key" style="background:${STAGES[index].color}"></i>${STAGES[index].name}</span><span class="stats-station-count">${value}<small>道</small></span><span class="stats-station-rule">${STAGES[index].rule}</span></button>`).join('');
    const due=count(reminder?.due_count),overdue=count(reminder?.overdue_count);
    const next=(Array.isArray(d.learning_actions)?d.learning_actions:[]).filter(item=>!/mode=spaced/.test(String(item.href||''))).slice(0,2)
      .map(item=>`<a href="${escapeHTML(safeLocalURL(item.href)||'#/review')}"><span><strong>${escapeHTML(item.title)}</strong><span>${escapeHTML(item.message)}</span></span>${icon('arrowRight')}</a>`).join('');
    const today=due
      ?`<div><div class="stats-today-label">今日间隔复习</div><div class="stats-today-count">${due}<small>道到期</small></div></div>${overdue?`<span class="stats-overdue">${icon('circleAlert')}其中逾期 ${overdue} 道</span>`:''}<button type="button" class="btn btn-p" data-action="open-spaced-review"${reviewScopeAttributes(subjectId,chapterId)} aria-label="打开间隔复习，今日待复习 ${due} 道">${icon('repeat')}开始间隔复习</button>`
      :`<div class="stats-clear"><span class="stats-bear${entering?' is-waking':''}" role="img" aria-label="小熊醒着"></span><div>今日间隔复习已完成<span>到期题目已全部处理</span></div></div>`;
    host.className='stats-hero'+(entering?' is-entering':'');
    host.innerHTML=`<div class="stats-hero-main">
      <div class="stats-hero-head"><div><div class="stats-hero-label" id="stats-hero-title">当前掌握率</div><div class="stats-figure">${rate===null?'<strong>—</strong>':`<strong data-count="${rate}">${rate}<small>%</small></strong>`}<span>已掌握 <b>${count(s.mastered)}</b> / ${count(s.total)} 道 ${deltaHTML}</span></div></div>${spark}</div>
      <div class="stats-pipe" role="group" aria-label="四轮间隔复习进度">
        <div class="stats-pipe-bar">${segments||'<span class="stats-pipe-seg is-empty" style="flex-grow:1"></span>'}</div>
        <svg class="stats-pipe-links" aria-hidden="true" focusable="false"></svg>
        <div class="stats-stations">${stations}</div>
        <div class="stats-pipe-foot"><span>${icon('rotateCcw')}本期 <b>${count(cycle.reset_mistakes)}</b> 道答错后回到第1轮</span><span>提示后完成 <b>${count(s.assisted_count)}</b> 次，留在原轮次次日再练</span><span>本期按四轮新掌握 <b>${count(cycle.normal_mastered)}</b> 道</span></div>
      </div></div>
      <aside class="stats-today" aria-label="今日">${today}${next?`<nav class="stats-next" aria-label="其他练习">${next}</nav>`:''}</aside>`;
    layoutPipe();
    if(entering)countUp(host.querySelector('[data-count]'));
    const pipe=host.querySelector('.stats-pipe');
    const mark=stage=>{
      pipe.classList.toggle('is-dimmed',stage!==null);
      pipe.querySelectorAll('[data-stage]').forEach(element=>element.classList.toggle('is-on',element.dataset.stage===stage));
      pipe.querySelectorAll('.stats-pipe-links path').forEach(path=>path.classList.toggle('is-on',path.dataset.stage===stage));
    };
    pipe.onpointerover=event=>{const target=event.target.closest('[data-stage]');if(target)mark(target.dataset.stage)};
    pipe.onpointerleave=()=>mark(null);
    pipe.onfocusin=event=>{const target=event.target.closest('[data-stage]');if(target)mark(target.dataset.stage)};
    pipe.onfocusout=()=>mark(null);
  }
  // Leader curves join each bar segment to its station; they depend on rendered widths.
  function layoutPipe(){
    const pipe=byId('stats-hero')?.querySelector('.stats-pipe');
    if(!pipe)return;
    const box=pipe.getBoundingClientRect(),links=pipe.querySelector('.stats-pipe-links');
    links.innerHTML=[...pipe.querySelectorAll('.stats-pipe-seg[data-stage]')].map(segment=>{
      const station=pipe.querySelector(`.stats-station[data-stage="${segment.dataset.stage}"]`);
      if(!station)return '';
      const a=segment.getBoundingClientRect(),b=station.getBoundingClientRect();
      const x1=(a.left+a.width/2-box.left).toFixed(1),x2=(b.left+b.width/2-box.left).toFixed(1);
      return `<path d="M${x1},0 C${x1},14 ${x2},10 ${x2},24" data-stage="${segment.dataset.stage}" style="--len:${Math.ceil(Math.hypot(x2-x1,24)+12)}"/>`;
    }).join('');
  }
  function countUp(element){
    if(!element||!element.dataset.count||window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
    const target=Math.max(0,Number(element.dataset.count)),started=performance.now(),suffix=element.querySelector('small').outerHTML;
    // Background tabs may not advance animation frames; always land on the final value.
    let finished=false;
    const done=setTimeout(()=>{finished=true;element.innerHTML=target+suffix},900);
    const step=time=>{
      if(finished)return;
      const progress=Math.max(0,Math.min(1,(time-started)/700));
      element.innerHTML=Math.round(target*(1-Math.pow(1-progress,3)))+suffix;
      if(progress<1)requestAnimationFrame(step);else{finished=true;clearTimeout(done)}
    };
    requestAnimationFrame(step);
  }

  // Period ledger: four KPIs in one card; sparklines have at most 14 points.
  function ledger(d,trendRows){
    const s=d.summary||{},chunks=F.chunkTrend(trendRows);
    const share=(key,row)=>row.pass+row.again?row[key]/(row.pass+row.again):null;
    const average=s.average_reviews_to_mastery===null||s.average_reviews_to_mastery===undefined?null:Number(s.average_reviews_to_mastery);
    const metrics=[
      ['复习次数',`${count(s.practice_count)}<small>次</small>`,`正常复习 ${count(s.known_review_events)} · 提示后 ${count(s.assisted_count)}`,C.sparkline(chunks.map(row=>row.reviews))],
      ['本次答对率',statsPercent(s.review_pass_rate),`样本 ${count(s.review_sample)} 次正常复习`,C.sparkline(chunks.map(row=>share('pass',row)),{stroke:'var(--chart-pass)'})],
      ['再次答错率',statsPercent(s.again_rate),`${count(s.forgotten_mistakes)} 道需要重点巩固`,C.sparkline(chunks.map(row=>share('again',row)),{stroke:'var(--chart-again)'})],
      ['平均复习几次掌握',Number.isFinite(average)?`${average.toFixed(1)}<small>次</small>`:'样本不足',`样本 ${count(s.mastery_sample)} 道 · 最少 4 次`,'']
    ];
    byId('stat-box').innerHTML=metrics.map(([label,value,note,spark])=>`<div class="stats-kpi"><span class="stats-kpi-label">${label}</span><span class="stats-kpi-value${value==='样本不足'?' is-empty':''}">${value}</span>${spark?`<span aria-hidden="true">${spark}</span>`:''}<span class="stats-kpi-note">${escapeHTML(note)}</span></div>`).join('');
  }

  // Study rhythm: calendar and trend highlight each other on hover and focus.
  function rhythm(d,trendRows,{table=false}={}){
    const host=byId('stats-rhythm'),tools=byId('stats-rhythm-tools'),scope=d.scope;
    const heat=Array.isArray(d.heatmap)?d.heatmap:[];
    if(!heat.length){
      tools.innerHTML='';
      host.innerHTML=statsEmptyHTML('这段时间还没有复习记录','calendar','完成一次间隔复习后，这里会出现日历和趋势');
      return null;
    }
    const streak=F.studyStreaks(heat,scope);
    const toggleLabel=table?'切换回图表':'以表格查看';
    tools.innerHTML=`<div class="stats-streaks"><span class="stats-streak">${icon('sun')}${streak.label} <b>${streak.current}</b> 天</span><span class="stats-streak">活跃 <b>${streak.active}</b> / ${streak.days} 天</span><span class="stats-streak">最长连续 <b>${streak.longest}</b> 天</span></div><button type="button" class="stats-icon-btn" data-stats-table aria-pressed="${table}" title="${toggleLabel}" aria-label="${toggleLabel}">${icon(table?'chart':'table')}</button>`;
    const legend=F.OUTCOMES.map(item=>`<span><i class="stats-key" style="background:${item.color}"></i>${item.label}</span>`).join('')+'<span><i class="stats-key is-diamond" style="background:var(--chart-manual)"></i>手动调整</span>';
    host.innerHTML=`<div class="stats-rhythm-body"${table?' hidden':''}><div><div class="stats-sub">学习日历</div><div class="stats-cal-host"></div><div class="heatmap-legend" role="img" aria-label="学习强度从少到多"><span>少</span>${[0,1,2,3,4].map(level=>`<i data-level="${level}" aria-hidden="true"></i>`).join('')}<span>多</span></div></div>
      <div class="stats-trend"><div class="trend-head"><div class="stats-sub">复习结果 · ${F.STATS_GRANULARITY_LABELS[scope.granularity]||'按日'}</div><div class="activity-legend">${legend}</div></div><div class="trend-plot"></div>${trendRows.some(row=>row.estimated)?'<p class="trend-note">浅色柱：统计完整之前的估算记录</p>':''}</div></div><div class="stats-rhythm-table"${table?'':' hidden'}></div>`;
    const body=host.querySelector('.stats-rhythm-body'),calendarHost=host.querySelector('.stats-cal-host'),today=F.dayKey(new Date());
    let trendChart=null;
    const calendarOptions={from:scope.date_from,to:scope.date_to,today,heat,onActive:day=>trendChart?.link(day)};
    let calendarChart=null;
    if(!table){
      calendarChart=C.calendar(calendarHost,calendarOptions);
      // Long ranges get the full card width; redraw once the column has its final width.
      if(!calendarChart.planner&&count(scope.days)>182){body.classList.add('is-stacked');calendarChart=C.calendar(calendarHost,calendarOptions)}
      trendChart=C.trend(host.querySelector('.trend-plot'),{rows:trendRows,granularity:scope.granularity,onActive:period=>{
        if(!period){calendarChart.link([]);return}
        const days=[];
        for(let day=scope.date_from;day<=scope.date_to;day=F.addDays(day,1))if(F.bucketOf(day,scope.granularity)===period)days.push(day);
        calendarChart.link(days);
      }});
    }else{
      const rows=[['时期','复习','答对','提示后','再练','手动调整','新掌握'],...trendRows.filter(row=>count(row.reviews)||count(row.manual_changes)).map(row=>[F.periodLabel(row.period,scope.granularity),count(row.reviews),count(row.pass),count(row.assisted),count(row.again),count(row.manual_changes),count(row.first_mastered)])];
      host.querySelector('.stats-rhythm-table').innerHTML=C.tableHTML(rows,'复习趋势明细');
    }
    return {calendarChart,trendChart};
  }

  // Chapter progress grouped by subject, ordered by what is still left to master.
  function chapters(d,{expanded=false,limit=9}={}){
    const threshold=Number(d.insights?.find?.(item=>item.rule==='weak_chapters')?.metric?.threshold)||.6;
    const rows=(Array.isArray(d.chapter_rates)?d.chapter_rates:[]).map((item,order)=>({...item,order,
      left:Math.max(0,count(item.total)-count(item.mastered)),weak:count(item.total)>=5&&Number(item.mastery_rate)<threshold}))
      .sort((a,b)=>b.left-a.left||count(b.total)-count(a.total)||a.order-b.order);
    const weakCount=rows.filter(row=>row.weak).length;
    byId('chapter-chart-meta').textContent=`${rows.length} 个章节${weakCount?` · ${weakCount} 个低于 ${Math.round(threshold*100)}%`:''}`;
    if(!rows.length){byId('chapter-chart').innerHTML=statsEmptyHTML('当前范围没有章节题目','barsHorizontal');return}
    const shown=expanded?rows:rows.slice(0,limit),max=Math.max(1,...rows.map(row=>count(row.total)));
    const groups=[];
    for(const row of shown){
      let group=groups.find(item=>item.id===row.subject_id);
      if(!group){group={id:row.subject_id,name:row.subject,rows:[]};groups.push(group)}
      group.rows.push(row);
    }
    const subjectTotals=id=>{
      const all=rows.filter(row=>row.subject_id===id);
      const totalCount=all.reduce((sum,row)=>sum+count(row.total),0),mastered=all.reduce((sum,row)=>sum+count(row.mastered),0);
      return {rate:pct(mastered,totalCount),left:totalCount-mastered,ratio:totalCount?mastered/totalCount*100:0};
    };
    byId('chapter-chart').innerHTML=groups.map(group=>{
      const totals=subjectTotals(group.id),subject=escapeHTML(group.name||'未归类');
      return `<div class="chapter-group"><div class="chapter-subject"><span>${subject}</span><span class="mini-meter" role="img" aria-label="${subject}掌握 ${totals.rate}%"><i style="width:${totals.ratio.toFixed(1)}%"></i></span><small>掌握 ${totals.rate}% · 未掌握 ${totals.left} 道</small></div>${group.rows.map(item=>{
        const rate=Number(item.mastery_rate)||0;
        return `<button type="button" class="chapter-row" data-chapter-id="${safeInteger(item.chapter_id)}" data-subject-id="${safeInteger(item.subject_id)}" aria-haspopup="menu" aria-expanded="false" aria-label="${escapeHTML(item.chapter||'未归类')}，共 ${count(item.total)} 道，已掌握 ${count(item.mastered)} 道，掌握率 ${statsPercent(rate)}"><span class="chapter-name"><span>${escapeHTML(item.chapter||'未归类')}</span>${item.weak?`<em class="chapter-weak">${icon('circleAlert')}薄弱</em>`:''}</span><span class="chapter-track" style="width:${Math.max(8,count(item.total)/max*100).toFixed(1)}%"><i style="width:${(rate*100).toFixed(1)}%"></i></span><span class="chapter-left">还差 <b>${item.left}</b> 道</span></button>`;
      }).join('')}</div>`;
    }).join('')+(rows.length>limit?`<div class="chapter-more"><button type="button" class="btn btn-txt btn-xs" data-stats-expand>${expanded?'收起':`显示全部 ${rows.length} 个章节`}</button></div>`:'');
  }

  // Mistake reasons: the largest share is emphasised, the rest stay quiet.
  function reasons(d){
    const list=(Array.isArray(d.reason_breakdown)?d.reason_breakdown:[]).map(item=>({reason:String(item.reason||''),count:count(item.count)})).sort((a,b)=>b.count-a.count);
    const total=list.reduce((sum,item)=>sum+item.count,0),max=Math.max(1,...list.map(item=>item.count));
    byId('reason-chart-meta').textContent=`${total} 次标记`;
    byId('reason-chart').innerHTML=list.length&&total
      ?`<p class="reason-lede">本期最常见的是 <b>${escapeHTML(list[0].reason)}</b>，占全部标记的 ${pct(list[0].count,total)}%。</p><div class="reason-list">${list.map((item,index)=>`<div class="reason-row${index===0?' is-top':''}"><span title="${escapeHTML(item.reason)}">${escapeHTML(item.reason)}</span><span class="reason-bar"><i style="width:${(item.count/max*100).toFixed(1)}%"></i></span><span class="stats-num"><b>${item.count}</b> · ${pct(item.count,total)}%</span></div>`).join('')}</div>`
      :statsEmptyHTML('本期没有新增错因标记','pie');
  }

  // Insights: triggered rules first with a next step; normal and low-sample rules fold into one line each.
  function insights(d,{subjectId=null,chapterId=null}={}){
    const list=(Array.isArray(d.insights)?d.insights:[]).map(item=>({...item,status:item.status==='ok'?'normal':item.status}));
    const triggered=list.filter(item=>item.status==='triggered'),normal=list.filter(item=>item.status==='normal');
    const insufficient=list.filter(item=>!['triggered','normal'].includes(item.status));
    const threshold=Number(list.find(item=>item.rule==='weak_chapters')?.metric?.threshold)||.6;
    const weak=(Array.isArray(d.chapter_rates)?d.chapter_rates:[]).filter(row=>count(row.total)>=5&&Number(row.mastery_rate)<threshold)
      .sort((a,b)=>Number(a.mastery_rate)-Number(b.mastery_rate)).slice(0,4);
    const extra=item=>{
      if(item.rule==='overdue_reviews')return `<button type="button" class="btn btn-out btn-xs" data-action="open-spaced-review"${reviewScopeAttributes(subjectId,chapterId)}>${icon('repeat')}先处理逾期</button>`;
      if(item.rule==='weak_chapters')return weak.map(row=>`<button type="button" class="insight-chip" data-stats-focus-chapter="${safeInteger(row.chapter_id)}" data-stats-focus-subject="${safeInteger(row.subject_id)}">${escapeHTML(row.chapter||'未归类')} ${statsPercent(row.mastery_rate)}</button>`).join('');
      if(item.rule==='again_rate_rising'&&hasRates(item))return dumbbell(Number(item.metric.prior_rate),Number(item.metric.recent_rate));
      return '';
    };
    const againRule=normal.find(item=>item.rule==='again_rate_rising'&&hasRates(item));
    const cards=triggered.map(item=>{
      const more=extra(item);
      return `<article class="insight triggered">${icon('circleAlert')}<div><strong>${escapeHTML(item.title)}</strong>${problemPreviewHTML(item.message,'insight-message')}<small>样本 ${safeInteger(item.sample_size)} · ${escapeHTML(F.formatInsightRange(item.range))}</small></div>${more?`<div class="insight-extra">${more}</div>`:''}</article>`;
    }).join('');
    const calm=(items,className,iconName,suffix)=>items.length?`<div class="insight-calm ${className}">${icon(iconName)}<span>${items.map(item=>escapeHTML(item.title)).join('、')}：${suffix}</span></div>`:'';
    const againNote=againRule?`（再次答错 ${statsPercent(againRule.metric.prior_rate)} → ${statsPercent(againRule.metric.recent_rate)}）`:'';
    byId('stats-insights').innerHTML=list.length
      ?`<div class="insight-list">${cards}${calm(normal,'normal','circleCheck','状态正常'+againNote)}${calm(insufficient,'insufficient','circleDashed','样本不足，暂不判断')}</div>`
      :statsEmptyHTML('暂无可判断的规则','lightbulb');
  }
  function hasRates(item){
    const metric=item.metric||{};
    return metric.prior_rate!==null&&metric.prior_rate!==undefined&&metric.recent_rate!==null&&metric.recent_rate!==undefined;
  }
  function dumbbell(prior,recent){
    const x=value=>8+Math.max(0,Math.min(1,value))*184;
    return `<svg class="insight-dumbbell" width="200" height="30" role="img" aria-label="再次答错比例：前半段 ${statsPercent(prior)}，后半段 ${statsPercent(recent)}"><line x1="${x(prior).toFixed(1)}" x2="${x(recent).toFixed(1)}" y1="10" y2="10"/><circle cx="${x(prior).toFixed(1)}" cy="10" r="4.5" fill="var(--chart-quiet)" stroke="var(--card)" stroke-width="2"/><circle cx="${x(recent).toFixed(1)}" cy="10" r="4.5" fill="var(--chart-again)" stroke="var(--card)" stroke-width="2"/><text x="${x(prior).toFixed(1)}" y="27" text-anchor="middle">前半段 ${statsPercent(prior)}</text><text x="${x(recent).toFixed(1)}" y="27" text-anchor="middle">后半段 ${statsPercent(recent)}</text></svg>`;
  }

  function exam(d,{chapterId=null}={}){
    const result=d.exam_attempts||{};
    byId('exam-stats-meta').textContent=chapterId?`${count(result.attempts_with_scoped_answers)} 次含本章作答`:`${count(result.submitted_attempts)} 次作答`;
    if(!count(result.submitted_attempts)){byId('exam-stats').innerHTML=statsEmptyHTML('还没有已提交的试卷','examPaper','提交作答后，这里会显示正确率和易错章节');return}
    const meter=(label,value,note)=>{
      const width=value===null||value===undefined?0:Math.max(0,Math.min(1,Number(value)))*100;
      return `<div class="exam-meter"><div class="exam-meter-top"><span>${label}</span><strong>${statsPercent(value)}</strong></div><div class="exam-meter-track"><i style="width:${width.toFixed(1)}%"></i></div><small>${escapeHTML(note)}</small></div>`;
    };
    const errors=Array.isArray(result.chapter_error_rates)?result.chapter_error_rates.slice(0,6):[];
    byId('exam-stats').innerHTML=`<div class="exam-meters">${meter('题级正确率（含提示）',result.accuracy,`${count(result.answered_items)} 道题`)}${chapterId?'':meter('整卷平均得分率',result.average_score_rate,`${count(result.scored_attempts)} 次计分`)}</div>`+
      (errors.length?`<div class="stats-sub exam-errors-title">各章节错题率</div><div class="exam-errors">${errors.map(item=>`<div class="exam-error-row"><span title="${escapeHTML(item.chapter||'未归类')}">${escapeHTML(item.chapter||'未归类')}</span><span class="exam-error-track"><i style="width:${(Math.max(0,Math.min(1,Number(item.error_rate)||0))*100).toFixed(1)}%"></i></span><span class="stats-num"><b>${statsPercent(item.error_rate)}</b> · ${count(item.incorrect)}/${count(item.answered)}</span></div>`).join('')}</div>`:'');
  }

  // Learning trail: most recently reviewed chapters with a one-click way back in.
  function trail(d,{subjectId=null}={}){
    const recent=Array.isArray(d.recent_chapters)?d.recent_chapters:[];
    if(!recent.length){byId('recent-list').innerHTML=statsEmptyHTML('这段时间没有复习记录','history');return}
    const subjectOf=new Map((Array.isArray(d.chapter_rates)?d.chapter_rates:[]).map(row=>[safeInteger(row.chapter_id),safeInteger(row.subject_id)]));
    const today=F.dayKey(new Date());
    const relative=stamp=>{
      const text=String(stamp||''),day=text.slice(0,10),time=escapeHTML(text.slice(11,16));
      if(!F.parseDay(day))return '';
      const gap=F.diffDays(day,today);
      return gap<=0?`今天<br>${time}`:gap===1?`昨天<br>${time}`:`${gap} 天前<br>${F.shortDay(day)}`;
    };
    byId('recent-list').innerHTML=`<div class="trail">${recent.map(item=>{
      const chapterId=safeInteger(item.chapter_id),scopeSubject=subjectOf.get(chapterId)||subjectId;
      const isToday=String(item.last_reviewed_at||'').slice(0,10)===today;
      return `<div class="trail-item${isToday?' is-today':''}"><span class="trail-time">${relative(item.last_reviewed_at)}</span><span class="trail-main"><b>${escapeHTML(item.chapter||'未归类')}</b><span>${escapeHTML(item.subject||'')} · 复习 ${count(item.review_count)} 次</span></span><button type="button" class="btn btn-txt btn-xs" data-action="open-spaced-review"${reviewScopeAttributes(scopeSubject,scopeSubject&&chapterId?chapterId:null)}>${icon('repeat')}继续</button></div>`;
    }).join('')}</div>`;
  }

  window.Cuoti.stats.render=Object.freeze({skeleton,completeness,hero,layoutPipe,ledger,rhythm,chapters,reasons,insights,exam,trail});
})();
