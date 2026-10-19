(function initDailyReviewRender(){
  'use strict';
  const feature=window.Cuoti.dailyReview;
  const CHOICES=Object.freeze(['solo','hint','again','incomplete']);
  const OUTCOMES=Object.freeze({
    solo:{label:'独立答对',icon:'check'},
    hint:{label:'提示后答对',icon:'lightbulb'},
    correct_unknown:{label:'答对（未注明提示）',icon:'check'},
    again:{label:'再次答错',icon:'rotateCcw'},
    incomplete:{label:'未完成',icon:'clock'},
    pending:{label:'待记录',icon:'circleDashed'}
  });
  const PURPOSES=Object.freeze({self_test:'自测',guided:'带提示练习'});
  function reasons(value){
    const values=Array.isArray(value)?value:String(value||'').split(',');
    return values.map(item=>String(item||'').trim()).filter(Boolean);
  }
  function normalize(item){
    return {
      id:safeInteger(item?.id),
      roundId:safeInteger(item?.round_id??item?.roundId),
      mistakeId:safeInteger(item?.mistake_id),
      publicId:String(item?.public_id||''),
      subjectId:safeInteger(item?.subject_id,''),
      chapterId:safeInteger(item?.chapter_id,''),
      subject:String(item?.subject||''),
      chapter:String(item?.chapter||''),
      qnum:String(item?.qnum||'未填写'),
      content:String(item?.content||''),
      note:String(item?.note||''),
      dateAdded:String(item?.date_added||''),
      reasons:Object.freeze(reasons(item?.reasons)),
      result:item?.completion_result??item?.result??null,
      assistance:item?.assistance||'unknown',
      pending:Boolean(item?.pending),
      legacyContent:Boolean(item?.legacy_live_content),
      readOnly:Boolean(item?.read_only),
      classification:item?.classification||{status:'historical_unknown'}
    };
  }
  function outcome(result,assistance){
    if(!result)return 'pending';
    if(result==='correct')return assistance==='hint'?'hint':assistance==='none'?'solo':'correct_unknown';
    return OUTCOMES[result]?result:'pending';
  }
  function outcomeLabel(key){return (OUTCOMES[key]||OUTCOMES.pending).label;}
  function resultLabel(value){
    return value==='correct'?'答对':value==='again'?'再次答错':value==='incomplete'?'未完成':'未记录';
  }
  function evidenceKey(item){return `daily:${item.roundId}:${item.id}`;}
  function chip(key,queued=false){
    if(queued)return `<span class="daily-result-chip is-queued">${icon('clock')}即将记录：${escapeHTML(outcomeLabel(key))}</span>`;
    return `<span class="daily-result-chip daily-result-${key}">${icon((OUTCOMES[key]||OUTCOMES.pending).icon)}${escapeHTML(outcomeLabel(key))}</span>`;
  }
  function meta(item){
    return `<span class="daily-q-meta"><strong>${escapeHTML(item.subject||'其他')}</strong><span>${escapeHTML(item.chapter||'未分章节')}</span><span class="daily-review-qnum">${escapeHTML(item.qnum)}</span>${item.legacyContent?'<span class="badge" title="原题面快照缺失，显示升级时保留的题面">旧批次参考题面</span>':''}</span>`;
  }
  function hintPanel(item,open){
    const typed=item.classification||{};
    const badges=(values,extra='')=>(values||[]).map(value=>`<span class="badge daily-hint-badge${extra}">${escapeHTML(value)}</span>`).join('');
    const row=(label,value)=>value?`<div class="daily-hint-row"><span>${label}</span><div>${value}</div></div>`:'';
    const annotations=typed.method_annotations;
    const methodRows=annotations?[
      ['主要方法',annotations.filter(item=>item.role==='primary')],
      ['辅助方法',annotations.filter(item=>item.role==='auxiliary')],
      ['未区分 / 待归类',annotations.filter(item=>item.role==='unspecified')]
    ].map(([label,items])=>row(label,badges(items.map(item=>item.name)))).join(''):row('方法（旧标注）',badges(typed.methods));
    return `<div class="daily-hint-panel" id="daily-hints-${item.id}"${open?'':' hidden'}>
      ${row('分类',item.readOnly?escapeHTML(typed.type_name||'未记录分类'):(window.Cuoti.classification?.inline(item)||''))}
      ${row('知识点',badges(typed.knowledge_points))}
      ${methodRows}
      ${row('原错因',badges(item.reasons,' badge-r'))}
      ${item.note?row('备注',`<span class="problem-note math">${escapeHTML(item.note)}</span>`):''}
    </div>`;
  }
  function hintToggle(item,open){
    return `<button type="button" class="daily-hint-toggle" data-daily-hint="${item.id}" aria-expanded="${open}" aria-controls="daily-hints-${item.id}">${icon('lightbulb')}<span>提示与分类</span>${icon('chevronDown','daily-hint-chevron')}</button>`;
  }
  function recordButtons(item,index,hintUsed){
    const buttons=CHOICES.map((choice,position)=>{
      const classes=['daily-rec-btn','js-daily-review-result'];
      if(hintUsed&&choice==='hint')classes.push('is-suggested');
      if(hintUsed&&choice==='solo')classes.push('is-muted');
      return `<button type="button" class="${classes.join(' ')}" data-item-id="${item.id}" data-result="${choice}">${icon(OUTCOMES[choice].icon)}<span>${OUTCOMES[choice].label}</span><kbd aria-hidden="true">${position+1}</kbd></button>`;
    }).join('');
    const note=`<span class="daily-viewed-note"${hintUsed?'':' hidden'}>已查看提示，答对建议记为「提示后答对」</span>`;
    return `<div class="daily-rec" role="group" aria-label="记录第 ${index} 题结果">${buttons}${note}</div>`;
  }
  function card(item,view={}){
    const index=view.index||1;
    const hintOpen=Boolean(view.hintOpen);
    const queued=view.queued||null;
    const done=Boolean(item.result)||Boolean(queued);
    const content=`<div class="daily-review-content">${problemPreviewHTML(item.content,'problem-preview-review')}</div>`;
    if(item.readOnly){
      return `<article class="daily-q" data-daily-review-item="${item.id}"><div class="daily-q-head"><span class="daily-q-num">${index}</span>${meta(item)}${chip(outcome(item.result,item.assistance))}</div>${content}${hintPanel(item,hintOpen)}<div class="daily-q-foot">${hintToggle(item,hintOpen)}<span class="daily-q-recorded">原题已被替换或移除 · 历史只读</span></div></article>`;
    }
    if(done){
      const key=queued?queued.choice:outcome(item.result,item.assistance);
      const open=Boolean(view.expanded)&&!queued;
      const typeName=item.classification?.type_name||'';
      return `<article class="daily-q is-done${open?' is-open':''}${queued?' is-queued':''}" data-daily-review-item="${item.id}" data-outcome="${key}">
        <button type="button" class="daily-q-head daily-q-toggle" data-daily-expand="${item.id}" aria-expanded="${open}" aria-controls="daily-q-detail-${item.id}"${queued?' disabled':''}>
          <span class="daily-q-num">${index}</span>${meta(item)}<span class="daily-q-preview">${escapeHTML(typeName)}</span>${chip(key,Boolean(queued))}${icon('chevronDown','daily-q-chevron')}
        </button>
        <div class="daily-q-detail" id="daily-q-detail-${item.id}"${open?'':' hidden'}>
          ${content}${hintPanel(item,hintOpen)}
          <div class="daily-q-foot">${hintToggle(item,hintOpen)}<span class="daily-q-recorded">结果已记录${item.assistance==='unknown'&&item.result?' · 辅助情况未记录':''}</span></div>
        </div>
      </article>`;
    }
    const current=Boolean(view.current);
    return `<article class="daily-q is-pending${current?' is-current':''}" data-daily-review-item="${item.id}" tabindex="-1" aria-label="第 ${index} 题">
      <div class="daily-q-head"><span class="daily-q-num">${index}</span>${meta(item)}<span class="daily-q-status">待记录</span></div>
      ${content}${hintPanel(item,hintOpen)}
      <div class="daily-q-foot">${hintToggle(item,hintOpen)}${recordButtons(item,index,Boolean(view.hintUsed))}<button type="button" class="btn btn-out btn-sm daily-pick" data-daily-pick="${item.id}">记录这道题</button></div>
    </article>`;
  }
  function list(items,viewFor,options={}){
    if(!items.length){
      if(options.hasSession)return emptyStateHTML('这个筛选下没有题目',{icon:'filter',compact:true,description:'换一个结果标签或清除筛选试试。'});
      return `<div class="empty-state daily-empty"><div class="empty-state-icon">${icon('sun')}</div><div class="empty-state-copy"><strong>还没有复习批次</strong><span>生成第一批后，题目会出现在这里；打印作答后回来逐题记录结果。</span></div><button type="button" class="btn btn-p btn-sm" data-daily-open="generate">${icon('plus')}生成第一批</button></div>`;
    }
    if(options.grouped){
      const groups=new Map();
      items.forEach(item=>{const key=item.classification?.type_name||'未记录分类';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item)});
      return [...groups].map(([name,rows])=>`<section class="review-type-group"><h3>${escapeHTML(name)} <small>${rows.length} 道</small></h3><div class="daily-review-list">${rows.map(item=>card(item,viewFor(item))).join('')}</div></section>`).join('');
    }
    return `<div class="daily-review-list">${items.map(item=>card(item,viewFor(item))).join('')}</div>`;
  }
  const SEGMENTS=Object.freeze(['solo','hint','correct_unknown','again','incomplete']);
  function progress(counts){
    const total=counts.total||0;
    if(!total)return '';
    const done=total-(counts.pending||0);
    const segments=SEGMENTS.filter(key=>counts[key]).map(key=>`<i class="daily-seg daily-seg-${key}" style="flex-grow:${safeInteger(counts[key])}"></i>`).join('')+(counts.pending?`<i class="daily-seg daily-seg-pending" style="flex-grow:${safeInteger(counts.pending)}"></i>`:'');
    const legend=SEGMENTS.filter(key=>key!=='correct_unknown'||counts[key]).map(key=>`<span><i class="daily-dot daily-seg-${key}"></i>${escapeHTML(outcomeLabel(key))} <b>${safeInteger(counts[key])}</b></span>`).join('')+`<span><i class="daily-dot daily-seg-pending"></i>待记录 <b>${safeInteger(counts.pending)}</b></span>`;
    const hint=counts.pending?`还剩 ${safeInteger(counts.pending)} 道<span class="daily-desk-only"> · 按 1–4 快速记录</span>`:'全部已记录';
    return `<div class="daily-progress-head"><strong>${done}<small>/ ${total} 已记录</small></strong><span class="daily-progress-hint">${hint}</span></div>
      <div class="daily-progress-track" role="img" aria-label="已记录 ${done} / ${total} 道">${segments}</div>
      <div class="daily-progress-legend">${legend}</div>`;
  }
  const CHIPS=Object.freeze([['all','全部'],['pending','待记录'],['solo','独立答对'],['hint','提示后答对'],['correct_unknown','答对（未注明提示）'],['again','再次答错'],['incomplete','未完成']]);
  function chips(counts,active){
    if(!counts.total)return '';
    return CHIPS.filter(([key])=>key!=='correct_unknown'||counts[key]).map(([key,label])=>{
      const count=key==='all'?counts.total:safeInteger(counts[key]);
      const selected=active===key;
      const disabled=!selected&&key!=='all'&&!count;
      return `<button type="button" class="daily-filter-chip${selected?' is-on':''}" data-daily-chip="${key}" aria-pressed="${selected}"${disabled?' disabled':''}>${escapeHTML(label)}<b>${count}</b></button>`;
    }).join('');
  }
  function finish(counts){
    const parts=['solo','hint','correct_unknown','again','incomplete'].filter(key=>key!=='correct_unknown'||counts[key]).map(key=>`${outcomeLabel(key)} ${safeInteger(counts[key])}`).join(' · ');
    return `<div class="daily-finish-card"><span class="daily-finish-icon">${icon('check')}</span><div class="daily-finish-text"><strong>本批已记录</strong><span>${escapeHTML(parts)}${counts.again||counts.hint?'　再次答错或提示后答对的题会进入「待巩固」':''}</span></div><div class="daily-finish-actions"><button type="button" class="btn btn-out btn-sm" data-daily-open="summary">${icon('chart')}看练习表现</button><button type="button" class="btn btn-p btn-sm" data-daily-open="generate">${icon('plus')}生成下一批</button></div></div>`;
  }
  function timeLabel(value){
    const date=new Date(value);
    if(!value||Number.isNaN(date.getTime()))return '';
    const today=new Date();
    const day=new Date(date.getFullYear(),date.getMonth(),date.getDate());
    const base=new Date(today.getFullYear(),today.getMonth(),today.getDate());
    const diff=Math.round((base-day)/86400000);
    const clock=date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
    if(diff===0)return `今天 ${clock} 生成`;
    if(diff===1)return `昨天 ${clock} 生成`;
    return `${date.getMonth()+1}月${date.getDate()}日 ${clock} 生成`;
  }
  function sessionMeta(session,typeName=''){
    if(!session)return '<span>按题型或方法抽一批错题，打印作答后回来逐题记录</span>';
    const selection=session.selection||{};
    const mode=selection.practice_kind==='remediation'?'复错再练':session.practice_mode==='focused'?`${selection.method_code?'方法':'题型'}专项${typeName?' · '+typeName:''}`:'混合复习';
    return [timeLabel(session.created_at),mode,PURPOSES[selection.pdf_purpose]||''].filter(Boolean).map(value=>`<span>${escapeHTML(value)}</span>`).join('');
  }
  function remediationRows(items){
    if(!items.length)return emptyStateHTML('暂无待巩固题目',{icon:'circleCheck',compact:true,description:'多次答错的题会出现在这里。'});
    return `<p class="daily-drawer-help">每批 1–10 道，今天到期的已默认勾选。</p>`+items.map((item,index)=>`<div class="daily-pick-row"><label class="classification-check"><input type="checkbox" data-remediation-id="${safeInteger(item.source_item_id)}"${item.due&&index<10?' checked':''}><span>${escapeHTML([item.subject,item.chapter,item.qnum].filter(Boolean).join(' · '))}</span>${item.due?'<span class="daily-due">到期</span>':''}</label><small>${escapeHTML(item.due_on||'')} · ${safeInteger(item.again_count)} 次需巩固${item.status==='已掌握'?' · 原已掌握':''}</small>${item.status==='已掌握'?`<button type="button" class="btn btn-out btn-xs" data-remediation-reopen="${escapeHTML(item.public_id)}">${icon('repeat')}重开间隔复习</button>`:''}</div>`).join('');
  }
  function summaryTable(items){
    if(!items.length)return '<p class="daily-drawer-help">该范围暂无记录。</p>';
    const modes={focused:'专项',mixed:'混合',remediation:'复错再练'};
    const percent=value=>value===null||value===undefined?null:Math.max(0,Math.min(100,Math.round(value*100)));
    const rows=items.map(row=>{
      const rate=percent(row.independent_accuracy);
      const completion=percent(row.completion_rate);
      const name=row.primary_type_code?`<a href="#/review?mode=daily&primary_type_code=${encodeURIComponent(row.primary_type_code)}" title="用此题型生成专项批次">${escapeHTML(row.type_name)}</a>`:`<span>${escapeHTML(row.type_name)}</span>`;
      const details=[`${safeInteger(row.unique_mistake_count)} 道题`,`完成作答 ${completion===null?'样本不足':completion+'%'}`,`借助提示 ${safeInteger(row.assisted_count)} · 未记录 ${safeInteger(row.unknown_count)}`,`待巩固 ${safeInteger(row.remediation_count)} · 已恢复 ${safeInteger(row.recovered_count)}`];
      return `<div class="daily-summary-row"><div class="daily-summary-name">${name}<small>${escapeHTML(modes[row.practice_mode]||row.practice_mode||'')}</small></div><div class="daily-summary-rate">${rate===null?'<span class="daily-muted">样本不足</span>':`<span class="daily-rate"><i style="width:${rate}%"></i></span><b>${rate}%</b>`}<small>独立 ${safeInteger(row.independent_sample_count)} 题</small></div><p class="daily-summary-detail">${details.map(escapeHTML).join('　')}</p></div>`;
    }).join('');
    return `<div class="daily-summary-list">${rows}</div><p class="daily-drawer-help">点击题型名称可直接生成该题型的专项批次。</p>`;
  }
  function methodSummary(result){
    const modes={focused:'专项练习',mixed:'混合复习',remediation:'复错再练'};
    const cards=(result.items||[]).map(row=>{
      const count=safeInteger(row.independent_sample_count);
      const rate=count>=5&&row.independent_accuracy!=null?Math.max(0,Math.min(100,Math.round(row.independent_accuracy*100))):null;
      const metrics=[['题目',row.unique_mistake_count,'道',''],['借助提示完成',row.assisted_count,'次',''],['待巩固',row.remediation_count,'道','is-attention'],['跨日恢复',row.recovered_count,'道','is-recovered']];
      return `<article class="daily-method-card"><div class="daily-method-head"><div class="daily-method-name"><h3>${escapeHTML(row.method_name)}</h3><p class="daily-method-meta"><span>${escapeHTML(row.subject)}</span><span class="daily-method-mode">${escapeHTML(modes[row.practice_mode]||row.practice_mode)}</span></p></div><div class="daily-method-rate">${rate===null?'<span class="daily-method-sample">样本不足</span>':`<b>${rate}<small>%</small></b><span>独立答对率</span><span>独立作答 ${count} 道</span>`}</div></div>
        ${rate===null?`<p class="daily-method-sample-note">独立作答 ${count} 道，满 5 道后显示独立答对率。</p>`:`<div class="daily-method-bar" aria-hidden="true"><i style="width:${rate}%"></i></div>`}
        <dl class="daily-method-stats">${metrics.map(([name,value,unit,style])=>`<div class="${value>0?style:''}"><dt>${name}</dt><dd>${safeInteger(value)}<small>${unit}</small></dd></div>`).join('')}</dl>
        <div class="daily-method-foot"><button type="button" class="btn btn-out btn-sm daily-method-practice" data-practice-method="${escapeHTML(row.method_code)}" data-subject-id="${safeInteger(row.subject_id)}" aria-label="练此方法：${escapeHTML(row.method_name)}">练此方法${icon('arrowRight')}</button></div></article>`;
    }).join('');
    const unknown=safeInteger(result.unattributed_count);
    return `<div class="daily-method-summary"><p class="daily-drawer-help">按批次保存的主要方法分组；同组的独立作答，每题取最近一次。</p>${cards?`<div class="daily-method-list">${cards}</div>`:emptyStateHTML('这个范围还没有方法记录',{icon:'chart',compact:true,description:'可换个范围，或切到「题型表现」。'})}${unknown?`<p class="daily-method-notice">${icon('info')}<span>另有 <b>${unknown}</b> 条练习记录缺少可用的主要方法，未计入上方统计。</span></p>`:''}<p class="daily-drawer-help">待巩固和跨日恢复反映当前巩固状态；跨日恢复也可能来自间隔复习。「练此方法」只预填新批次设置，确认后才生成。</p></div>`;
  }
  feature.render=Object.freeze({normalize,outcome,outcomeLabel,resultLabel,evidenceKey,card,list,progress,chips,finish,sessionMeta,remediationRows,summaryTable,methodSummary,CHOICES});
})();
