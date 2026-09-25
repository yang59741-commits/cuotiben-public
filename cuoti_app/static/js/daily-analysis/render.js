(function initDailyAnalysisRender(){
  'use strict';

  const feature=window.Cuoti.dailyAnalysis;

  function field(label,value,className=''){
    const text=String(value||'').trim();
    if(!text)return '';
    return `<div class="daily-analysis-field ${className}"><strong>${escapeHTML(label)}</strong><p class="math">${escapeHTML(text)}</p></div>`;
  }

  function formatDailyAnalysisTime(value){
    const date=new Date(value);
    return Number.isNaN(date.getTime())?String(value||'未知时间'):
      date.toLocaleString('zh-CN',{hour12:false});
  }

  function card(item){
    const analysis=item?.analysis||{};
    const stale=item?.stale
      ?'<span class="daily-analysis-state stale">题目内容已变化</span>'
      :'<span class="daily-analysis-state">已缓存</span>';
    return `<article class="daily-analysis-card" data-mistake-public-id="${escapeHTML(item?.mistake_public_id||'')}">
      <header><div><span class="daily-analysis-index">${safeInteger(item?.position)}.</span><strong>${escapeHTML(item?.subject||'其他')}</strong><span>${escapeHTML(item?.chapter||'未归类')}</span><small>${escapeHTML(item?.qnum||'未标号')}</small></div>${stale}</header>
      ${field('备注判断',analysis.note_assessment,'assessment')}
      ${field('必要纠正',analysis.correction,'correction')}
      ${field('解题提示',analysis.hint,'hint')}
      ${field('题型与命题模式',analysis.exam_pattern,'pattern')}
      ${window.Cuoti.classification?.inline(item)||''}
      <div class="analysis-learning-actions">${['auto','manual'].includes(item?.classification?.status)&&item.classification.primary_type_code?`<a class="btn btn-out btn-xs js-analysis-practice-type" href="#/review?mode=daily&primary_type_code=${encodeURIComponent(item.classification.primary_type_code)}">${icon('refresh')}练此题型</a>`:''}<input class="settings-input analysis-self-check" type="text" maxlength="200" aria-label="下次自查事项" placeholder="下次自查事项"><button type="button" class="btn btn-out btn-xs js-analysis-self-check">${icon('check')}记录自查</button></div>
      <footer>生成于 ${escapeHTML(formatDailyAnalysisTime(item?.generated_at))}</footer>
    </article>`;
  }

  function body(payload){
    const items=Array.isArray(payload?.items)?payload.items:[];
    const summary=[];
    if(payload?.stale_count)summary.push(`${safeInteger(payload.stale_count)} 项内容已变化`);
    if(payload?.missing_count)summary.push(`${safeInteger(payload.missing_count)} 项尚未生成`);
    const notice=summary.length
      ?`<div class="daily-analysis-notice">${icon('circleHelp')}${escapeHTML(summary.join(' · '))}</div>`:'';
    if(!items.length){
      return notice+emptyStateHTML('暂无可显示的分析',{icon:'sparkles',description:'可以重新生成当前范围的分析。',compact:true});
    }
    return `${notice}<div class="daily-analysis-list">${items.map(card).join('')}</div>`;
  }

  feature.render=Object.freeze({field,card,body,formatTime:formatDailyAnalysisTime});
})();
