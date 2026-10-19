(function initReviewRenderer(){
  'use strict';

  const review=window.Cuoti.review;

  function reasonList(value){
    const values=Array.isArray(value)?value:String(value||'').split(',');
    return values.map(reason=>String(reason||'').trim()).filter(Boolean);
  }

  function normalizeReviewItem(item){
    return Object.freeze({
      id:safeInteger(item?.id),
      publicId:String(item?.public_id||item?.publicId||''),
      subject:String(item?.subject||''),
      chapter:String(item?.chapter||''),
      qnum:String(item?.qnum||'无'),
      content:String(item?.content||''),
      note:String(item?.note||''),
      reasons:Object.freeze(reasonList(item?.reasons)),
      reviewCount:Math.max(0,safeInteger(item?.review_count??item?.reviewCount)),
      reviewStage:Math.max(0,Math.min(3,safeInteger(item?.review_stage??item?.reviewStage))),
      revision:safeInteger(item?.revision),
      dueDate:String(item?.next_review||item?.dueDate||''),
      dueStatus:String(item?.due_status||item?.dueStatus||'unknown'),
      overdueDays:Math.max(0,safeInteger(item?.overdue_days??item?.overdueDays)),
      pending:Boolean(item?.pending)
    });
  }

  function reviewProgressText(item){
    const stage=Math.max(0,Math.min(3,safeInteger(item.reviewStage)));
    const after=['5 天后再复习','10 天后再复习','15 天后再复习','完成本轮并掌握'][stage];
    return `第 ${stage+1}/4 轮 · 独立答对后${after}`;
  }

  function renderReviewActions(item,{compact=false}={}){
    const id=safeInteger(item.id);
    const size=compact?'btn-xs':'btn-sm';
    const disabled=item.pending?' disabled aria-disabled="true"':'';
    const actions=[
      {result:'pass',label:window.Cuoti.reviewEvidence?.value(`spaced:${item.publicId}:${item.revision}`)==='hint'?'提示后答对':'独立答对',iconName:'check',theme:'btn-s'},
      {result:'again',label:'需要再练',iconName:'refresh',theme:'btn-y'}
    ];
    return actions.map(action=>`<button type="button" class="btn ${action.theme} ${size} js-review" data-id="${id}" data-result="${action.result}"${action.result==='pass'?` data-evidence-pass="spaced:${escapeHTML(item.publicId)}:${item.revision}"`:''} title="${action.label}" aria-label="${action.label}"${disabled}>${icon(action.iconName)}${compact?'':`<span class="evidence-pass-label">${action.label}</span>`}</button>`).join('');
  }

  function renderReasonBadges(item,{empty=false}={}){
    if(!item.reasons.length)return empty?'<span class="problem-empty">未标记错因</span>':'';
    return item.reasons.map(reason=>`<span class="badge badge-r">${escapeHTML(reason)}</span>`).join(' ');
  }

  function renderReviewProblem(item){
    const note=item.note?`<div class="problem-note math">备注: ${escapeHTML(item.note)}</div>`:'';
    const key=`spaced:${item.publicId}:${item.revision}`;
    const progress=reviewProgressText(item);
    const due=item.dueStatus==='overdue'?`逾期 ${item.overdueDays} 天 · 原定 ${item.dueDate}`:item.dueStatus==='today'?`今日到期 · ${item.dueDate}`:'待复习 · 未记录到期日期';
    return `<span class="problem-number">${escapeHTML(item.qnum)}</span><span class="review-due-label${item.dueStatus==='overdue'?' overdue':''}">${escapeHTML(due)}</span>${problemPreviewHTML(item.content,'problem-preview-review')}<div class="review-stage-label" data-evidence-progress="${escapeHTML(key)}" data-independent-progress="${escapeHTML(progress)}">${escapeHTML(window.Cuoti.reviewEvidence?.value(key)==='hint'?'提示后完成不升级 · 明天再测':progress)}</div><details data-evidence-hints="${escapeHTML(key)}"><summary>备注与错因</summary>${note}${renderReasonBadges(item)}</details>${window.Cuoti.reviewEvidence?.control(key)||''}`;
  }

  function renderReviewRow(item){
    return `<tr data-review-id="${safeInteger(item.id)}"><td class="review-subject">${escapeHTML(item.subject)}</td><td>${escapeHTML(item.chapter)}</td><td class="problem-cell">${renderReviewProblem(item)}</td><td>${item.reasons.length?'已记录':'未标记'}</td><td class="review-count">${item.reviewCount}次</td><td class="act-cell review-actions-column"><span class="review-row-actions">${renderReviewActions(item,{compact:true})}</span></td></tr>`;
  }

  function renderReviewCard(item){
    return `<article class="review-card" data-review-id="${safeInteger(item.id)}">
      <div class="review-card-head"><strong>${escapeHTML(item.subject)}</strong><span>${escapeHTML(item.chapter)}</span><span class="review-card-count">复习 ${item.reviewCount} 次</span></div>
      <div>${renderReviewProblem(item)}</div>
      <div class="review-card-actions">${renderReviewActions(item)}</div>
    </article>`;
  }

  function renderReviewList(items){
    const tableRows=items.map(renderReviewRow).join('');
    const cards=items.map(renderReviewCard).join('');
    return `<div class="review-table-scroll"><table class="tb"><thead><tr><th>科目</th><th>章节</th><th>题目</th><th>错因</th><th>复习</th><th class="review-actions-column"><span class="sr-only">操作</span></th></tr></thead><tbody>${tableRows}</tbody></table></div><div class="review-card-list">${cards}</div>`;
  }

  function renderReviewStats(total,summary={}){
    return `<div class="stat s1"><div class="stat-icon">${icon('repeat')}</div><div class="stat-info"><div class="n">${Math.max(0,safeInteger(total))}</div><div class="l">今日待复习 · 含逾期 ${Math.max(0,safeInteger(summary.overdue))} 道</div></div></div><div class="stat s2"><div class="stat-icon">${icon('circleDashed')}</div><div class="stat-info"><div class="n">${Math.max(0,safeInteger(summary.active))}</div><div class="l">总未掌握</div></div></div><div class="stat s3"><div class="stat-icon">${icon('circleCheck')}</div><div class="stat-info"><div class="n">${Math.max(0,safeInteger(summary.mastered))}</div><div class="l">已掌握</div></div></div>`;
  }

  review.render=Object.freeze({
    normalize:normalizeReviewItem,
    progressText:reviewProgressText,
    actions:renderReviewActions,
    row:renderReviewRow,
    card:renderReviewCard,
    list:renderReviewList,
    stats:renderReviewStats
  });
})();
