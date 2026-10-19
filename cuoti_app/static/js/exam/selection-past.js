(function initSelectionPast(){
'use strict';
const examPage=window.Cuoti.exam;
let pastExamSourcesLoaded=false;
async function loadPastExamSources(){
  const card=document.getElementById('past-source-card');
  if(pastExamSourcesLoaded){
    renderPastExamSource();
    return;
  }
  card.innerHTML=`<div class="past-source-loading">${icon('loader','icon-spin')}正在读取逐年真题结构...</div>`;
  try{
    const data=await examPage.api.request('/api/past-exam-sources');
    if(!Array.isArray(data)){
      card.innerHTML=`<div class="past-source-error">${icon('circleAlert')} ${escapeHTML(data?.error||'真题结构读取失败')}</div>`;
      return;
    }
    examPage.state.pastExamSources=data;
    pastExamSourcesLoaded=true;
    const institutionSelect=document.getElementById('ep-institution');
    if(!data.length){
      institutionSelect.replaceChildren(new Option('没有满足条件的真题来源',''));
      document.getElementById('ep-subject').replaceChildren(
        new Option('每个来源至少需要 1 个有效年份','')
      );
      card.innerHTML=emptyStateHTML('暂无可用真题来源',{icon:'fileText',description:'真题组卷需要同一院校、同一科目至少 1 个有效年份的正式 TeX。',compact:true});
      return;
    }
    const institutions=[...new Set(data.map(item=>String(item.institution||'其他院校')))]
      .sort((first,second)=>{
        const firstUsable=data.some(item=>
          String(item.institution||'其他院校')===first&&pastExamSourceUsable(item)
        );
        const secondUsable=data.some(item=>
          String(item.institution||'其他院校')===second&&pastExamSourceUsable(item)
        );
        return Number(secondUsable)-Number(firstUsable)||first.localeCompare(second,'zh-CN');
      });
    institutionSelect.replaceChildren();
    if(institutions.length){
      institutions.forEach(name=>institutionSelect.add(new Option(name,name)));
    }else{
      institutionSelect.add(new Option('没有可用院校',''));
    }
    loadPastExamSubjects();
  }catch(error){
    if(error?.name==='AbortError')return;
    card.innerHTML=`<div class="past-source-error">${icon('circleAlert')} 真题结构读取失败，请检查本地 TeX 目录。</div>`;
  }
}

function loadPastExamSubjects(){
  const institution=document.getElementById('ep-institution').value;
  const select=document.getElementById('ep-subject');
  const sources=examPage.state.pastExamSources
    .filter(item=>String(item.institution)===institution)
    .sort((first,second)=>
      Number(pastExamSourceUsable(second))-Number(pastExamSourceUsable(first))||
      String(first.subject||'').localeCompare(String(second.subject||''),'zh-CN')
    );
  select.replaceChildren();
  if(sources.length){
    sources.forEach(item=>{
      const usable=pastExamSourceUsable(item);
      const option=new Option(
        String(item.subject??'')+(usable?'':'（暂无可提交画像）'),
        String(safeInteger(item.subject_id,''))
      );
      option.title=usable?'':pastExamAvailabilityMessage(item);
      select.add(option);
    });
  }else{
    select.add(new Option('该院校暂无可用科目',''));
  }
  renderPastExamSource();
}

function pastExamAvailabilityMessage(value){
  const profile=value?.profile_availability;
  const message=String(profile?.message||'').trim();
  if(message)return message;
  const reason=String(
    value?.availability_reason??profile?.reason??profile?.code??''
  ).trim();
  const labels={
    missing_active_profile_v3:'尚未激活该组合的正式画像',
    profile_abandoned:'画像建设已停止；现有有效题目仍保留为通用候选',
    unsupported_profile_combination:'该院校、科目和年份组合暂不支持提交',
    source_unavailable:'真题 TeX 结构不可用',
    past_source_unavailable:'真题 TeX 结构不可用',
    generation_submission_not_supported:'该组合暂未开放正式组卷'
  };
  return labels[reason]||reason||'该组合当前不可提交';
}

function pastExamSourceUsable(source){
  return source?.available===true&&
    source?.usable===true&&
    source?.generation_submission_supported===true&&
    pastExamYearStructures(source).some(structure=>structure.usable);
}

function pastExamYearStructures(source){
  const rawStructures=Array.isArray(source?.year_structures)
    ?source.year_structures
    :Array.isArray(source?.structures)
      ?source.structures
      :[];
  const byYear=new Map();
  rawStructures.forEach(raw=>{
    const year=safeInteger(raw?.year);
    if(!year)return;
    const warning=Array.isArray(raw?.warnings)
      ?raw.warnings.map(item=>String(item??'').trim()).filter(Boolean).join('；')
      :String(raw?.warning??raw?.anomaly??'').trim();
    const sourceAvailable=raw?.source_available===true;
    const profileSelectable=raw?.profile_selectable===true;
    const usable=sourceAvailable&&profileSelectable&&
      raw?.available===true&&raw?.usable===true;
    const sourceModes=raw?.source_modes&&typeof raw.source_modes==='object'
      ?raw.source_modes:{};
    byYear.set(year,{
      year,
      actual_reference_year:safeInteger(raw?.actual_reference_year??year),
      question_count:Math.max(0,safeInteger(raw?.question_count??raw?.count)),
      sections:Array.isArray(raw?.sections)?raw.sections:[],
      source_available:sourceAvailable,
      profile_selectable:profileSelectable,
      usable,
      availability_reason:usable?'':pastExamAvailabilityMessage(raw),
      year_selection_reason:String(raw?.year_selection_reason??'').trim(),
      learning_years:Array.isArray(raw?.learning_years)?raw.learning_years.map(value=>safeInteger(value)).filter(Boolean):[],
      warning,
      filename:String(raw?.filename??'').trim(),
      source_modes:sourceModes
    });
  });
  const fallbackYears=Array.isArray(source?.available_years)?source.available_years:[];
  const selectableYears=new Set(
    (Array.isArray(source?.selectable_years)?source.selectable_years:[])
      .map(value=>safeInteger(value)).filter(Boolean)
  );
  const sourceAvailableYears=new Set(
    (Array.isArray(source?.source_available_years)?source.source_available_years:[])
      .map(value=>safeInteger(value)).filter(Boolean)
  );
  fallbackYears.forEach(rawYear=>{
    const year=safeInteger(rawYear);
    if(!year||byYear.has(year))return;
    const isLatest=year===safeInteger(source?.latest_year);
    const sourceAvailable=sourceAvailableYears.has(year);
    const profileSelectable=selectableYears.has(year);
    const usable=sourceAvailable&&profileSelectable&&
      source?.available===true&&source?.usable===true&&
      source?.generation_submission_supported===true;
    byYear.set(year,{
      year,
      actual_reference_year:safeInteger(source?.actual_reference_year??year),
      question_count:isLatest?Math.max(0,safeInteger(source?.question_count)):0,
      sections:isLatest&&Array.isArray(source?.sections)?source.sections:[],
      source_available:sourceAvailable,
      profile_selectable:profileSelectable,
      usable,
      availability_reason:usable?'':pastExamAvailabilityMessage(source),
      warning:'',
      filename:isLatest?String(source?.filename??'').trim():''
    });
  });
  if(!byYear.size&&safeInteger(source?.latest_year)){
    const year=safeInteger(source.latest_year);
    const sourceAvailable=sourceAvailableYears.has(year);
    const profileSelectable=selectableYears.has(year);
    const usable=sourceAvailable&&profileSelectable&&
      source?.available===true&&source?.usable===true&&
      source?.generation_submission_supported===true;
    byYear.set(year,{
      year,
      actual_reference_year:safeInteger(source?.actual_reference_year??year),
      question_count:Math.max(0,safeInteger(source?.question_count)),
      sections:Array.isArray(source?.sections)?source.sections:[],
      source_available:sourceAvailable,
      profile_selectable:profileSelectable,
      usable,
      availability_reason:usable?'':pastExamAvailabilityMessage(source),
      warning:'',
      filename:String(source?.filename??'').trim()
    });
  }
  return [...byYear.values()].sort((first,second)=>second.year-first.year);
}

function updatePastCandidateNote(questionCount,institution){
  const note=document.getElementById('past-candidate-note');
  const select=document.getElementById('ep-candidate-source');
  if(!note||!select)return;
  const mode=select.value;
  if(mode==='other_past'){
    note.textContent=`全部 ${questionCount||'所需'} 题来自其他院校同科真题，严格排除${institution||'当前目标院校'}，原题默认不改编。`;
    note.className='past-candidate-note is-past';
    return;
  }
  if(mode==='mixed'){
    const ordinaryCount=Math.ceil(questionCount/2);
    const pastCount=Math.floor(questionCount/2);
    note.textContent=questionCount
      ?`混合筛选：普通题库 ${ordinaryCount} 题、其他院校同科真题 ${pastCount} 题；严格排除${institution||'当前目标院校'}。`
      :'普通题库与其他院校同科真题约各占一半；至少需要 2 题。';
    note.className='past-candidate-note is-mixed';
    return;
  }
  note.textContent=`全部 ${questionCount||'所需'} 题从普通题库筛选，并按结构年份保持章节配额。`;
  note.className='past-candidate-note is-ordinary';
}

function renderPastExamSource(){
  const select=document.getElementById('ep-subject');
  const institution=document.getElementById('ep-institution').value;
  const card=document.getElementById('past-source-card');
  const yearSelect=document.getElementById('ep-reference-year');
  const source=examPage.state.pastExamSources.find(item=>
    String(item.institution)===institution&&
    Number(item.subject_id)===Number(select.value)
  );
  if(!source){
    yearSelect.replaceChildren(new Option('请先选择院校和科目',''));
    yearSelect.dataset.sourceKey='';
    updatePastCandidateNote(0,institution);
    card.innerHTML=emptyStateHTML('暂无正式真题',{icon:'fileText',description:'当前科目没有可用于组卷的正式真题 TeX。',compact:true});
    return;
  }
  const structures=pastExamYearStructures(source);
  const sourceKey=`${institution}\u0000${safeInteger(source.subject_id)}`;
  const previousYear=yearSelect.dataset.sourceKey===sourceKey?safeInteger(yearSelect.value):0;
  yearSelect.replaceChildren();
  structures.forEach(structure=>{
    const reason=structure.usable?'':structure.availability_reason;
    const label=`${structure.year} 年${structure.usable?'':`（${reason}）`}`;
    const option=new Option(label,String(structure.year));
    option.disabled=!structure.usable;
    option.title=reason;
    yearSelect.add(option);
  });
  const usableStructures=structures.filter(structure=>structure.usable);
  const defaultYear=usableStructures.find(structure=>structure.year===safeInteger(source.latest_year))?.year
    ??usableStructures[0]?.year
    ??0;
  const selectedYear=usableStructures.some(structure=>structure.year===previousYear)
    ?previousYear
    :defaultYear;
  yearSelect.value=selectedYear?String(selectedYear):'';
  yearSelect.dataset.sourceKey=sourceKey;
  if(!structures.length||!selectedYear){
    if(!structures.length)yearSelect.add(new Option('没有可用结构年份',''));
    updatePastCandidateNote(0,institution);
    const diagnostics=structures
      .map(item=>`${item.year} 年：${item.availability_reason}`)
      .filter(value=>!value.endsWith('：'));
    card.innerHTML=diagnostics.length
      ?`<div class="past-source-error">${icon('circleAlert')}<span>${escapeHTML(diagnostics.join('；'))}</span></div>`
      :emptyStateHTML('暂无逐年结构',{icon:'fileText',description:'当前科目还没有可用于组卷的逐年真题结构。',compact:true});
    return;
  }
  const structure=structures.find(item=>item.year===selectedYear)||usableStructures[0];
  const sections=(structure.sections||[]).map((section,index)=>`
    <span class="past-section-chip">
      <span class="past-section-index">${index+1}</span>
      <span>${escapeHTML(section.label||section.name||`板块 ${index+1}`)}</span>
      <strong>${Math.max(0,safeInteger(section.question_count??section.count))}题</strong>
    </span>`).join('');
  const questionCount=Math.max(0,safeInteger(structure.question_count));
  const learningYears=structure.learning_years||[];
  const modeInfo=structure.source_modes?.[document.getElementById('ep-candidate-source')?.value]
    ||{status:'not_run',acceptance_status:'not_run'};
  const filename=structure.filename||String(source.filename??'');
  const warnings=structures.flatMap(item=>[
    String(item.warning??'').trim()
      ?`${item.year} 年：${String(item.warning).trim()}`
      :'',
    !item.usable&&item.availability_reason
      ?`${item.year} 年：${item.availability_reason}`
      :''
  ]).filter(Boolean);
  updatePastCandidateNote(questionCount,institution);
  card.innerHTML=`
    <div class="past-source-head">
      <span class="past-source-year">${selectedYear}<small>结构年份</small></span>
      <div>
        <strong>${escapeHTML(source.institution)} · ${escapeHTML(source.subject)} · ${questionCount} 题</strong>
        <p title="${escapeHTML(filename)}">${escapeHTML(filename||'正式真题 TeX')}</p>
      </div>
    </div>
    <div class="past-source-rule">${icon('listChecks')}${learningYears.length?`学习 ${escapeHTML(learningYears.join('、'))} 年真题；`:''}沿用 ${selectedYear} 年板块顺序与题量</div>
    ${structure.year_selection_reason?`<div class="past-source-warning">${icon('triangleAlert')}<span>${escapeHTML(structure.year_selection_reason)}</span></div>`:''}
    ${warnings.length?`<div class="past-source-warning">${icon('triangleAlert')}<span>${escapeHTML(warnings.join('；'))}</span></div>`:''}
    <div class="past-source-rule">${icon('shieldCheck')}候选题源状态：${modeInfo&&modeInfo.available===false
      ?`不可用：${escapeHTML(String(modeInfo.reason||'该题源方式当前不可用'))}`
      :escapeHTML(String(modeInfo.acceptance_status||modeInfo.status||'not_run'))}</div>
    <div class="past-section-list">${sections||'<span class="past-section-empty">该年份未提供板块明细</span>'}</div>
    <div class="past-source-years">可选结构年份：${escapeHTML(usableStructures.map(item=>item.year).join('、'))}</div>`;
}


examPage.selectionPast=Object.freeze({loadPastExamSources,loadPastExamSubjects,pastExamAvailabilityMessage,pastExamSourceUsable,pastExamYearStructures,updatePastCandidateNote,renderPastExamSource});
})();
