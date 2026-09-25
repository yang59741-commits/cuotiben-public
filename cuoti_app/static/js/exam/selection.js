(function initExamSelection(){
'use strict';
const examPage=window.Cuoti.exam;

const MAX_EXAM_MISTAKE_REFERENCES=20;

const availableExamChapterIds=new Set();

let examChapterRequestRevision=0;

let examChapterRequestController=null;

let examBankCoverageRequestRevision=0;

let examBankCoverageRequestController=null;

function isExcludedExamChapter(chapter){
  const name=String(chapter?.name??'').replace(/\s+/g,'');
  return name.includes('场论初步');
}

function resetExamBankCoverage(message='选择科目后显示有效题库覆盖情况',state='idle'){
  if(examBankCoverageRequestController)examBankCoverageRequestController.abort();
  examBankCoverageRequestRevision+=1;
  const card=document.getElementById('exam-bank-coverage');
  if(!card)return;
  card.className='exam-bank-coverage is-'+state;
  card.replaceChildren();
  const text=document.createElement('span');
  text.className='exam-bank-coverage-message';
  text.textContent=message;
  card.append(text);
}

function renderExamBankCoverage(payload){
  const card=document.getElementById('exam-bank-coverage');
  if(!card)return;
  const status=String(payload?.status??'ready').toLowerCase();
  const errors=Array.isArray(payload?.errors)
    ?payload.errors.map(error=>String(error?.message??error?.error??error??'').trim()).filter(Boolean)
    :payload?.errors?[String(payload.errors)]:[];
  const total=Math.max(0,safeInteger(
    payload?.total_questions??payload?.valid_question_count??payload?.valid_count
  ));
  const duplicate=Math.max(0,safeInteger(
    payload?.duplicate_count??payload?.duplicate_question_count
  ));
  const excluded=Math.max(0,safeInteger(
    payload?.excluded_count??payload?.excluded_question_count
  ));
  const chapters=Array.isArray(payload?.chapters)?payload.chapters:[];
  const chapterCount=chapters.length||examPage.state.selectedExamChapters.size||availableExamChapterIds.size;
  const snapshot=String(payload?.snapshot_id??payload?.snapshot??'').trim();
  const isError=errors.length>0||['error','failed','blocked','invalid'].includes(status);
  const isPending=['building','pending','updating','loading'].includes(status);
  card.className='exam-bank-coverage '+(isError?'is-error':isPending?'is-loading':'is-ready');

  if(isError){
    const detail=errors.length?errors.join('；'):'有效题库索引不可用，请检查题库文件';
    card.innerHTML=`
      <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
      <span class="exam-bank-coverage-message"><strong>题库检查未通过</strong>${escapeHTML(detail)}</span>`;
    return;
  }
  if(isPending){
    card.innerHTML=`
      <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
      <span class="exam-bank-coverage-message"><strong>题库索引正在更新</strong>完成后会自动用于组卷</span>`;
    return;
  }

  const scope=examPage.state.selectedExamChapters.size?'当前所选章节':'当前科目';
  const snapshotLabel=snapshot
    ?`索引 ${escapeHTML(snapshot.length>12?snapshot.slice(0,12)+'…':snapshot)}`
    :'索引已校验';
  card.innerHTML=`
    <span class="exam-bank-coverage-dot" aria-hidden="true"></span>
    <span class="exam-bank-coverage-message">
      <strong>${scope}可用 ${total} 题</strong>
      覆盖 ${chapterCount} 个章节 · 排除 ${excluded} 题 · 去重 ${duplicate} 题
    </span>
    <span class="exam-bank-coverage-snapshot" title="${escapeHTML(snapshot||'有效题库索引已校验')}">${snapshotLabel}</span>`;
}

async function loadExamBankCoverage(){
  const subjectSelect=document.getElementById('e-subject');
  const sid=subjectSelect?.value||'';
  if(!sid){
    resetExamBankCoverage();
    return;
  }
  const revision=++examBankCoverageRequestRevision;
  if(examBankCoverageRequestController)examBankCoverageRequestController.abort();
  examBankCoverageRequestController=new AbortController();
  const card=document.getElementById('exam-bank-coverage');
  if(card){
    card.className='exam-bank-coverage is-loading';
    card.innerHTML='<span class="exam-bank-coverage-dot" aria-hidden="true"></span><span class="exam-bank-coverage-message">正在核对有效题库...</span>';
  }
  const chapterIds=examPage.state.selectedExamChapters.size
    ?[...examPage.state.selectedExamChapters]
    :[...availableExamChapterIds];
  chapterIds.sort((first,second)=>first-second);
  const params=new URLSearchParams({
    subject_id:sid,
    chapter_ids:chapterIds.join(',')
  });
  try{
    const payload=await examPage.api.request(
      '/api/v2/question-bank/coverage?'+params.toString(),
      {signal:examBankCoverageRequestController.signal}
    );
    if(
      revision!==examBankCoverageRequestRevision||
      subjectSelect.value!==sid
    )return;
    renderExamBankCoverage(payload);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examBankCoverageRequestRevision)return;
    resetExamBankCoverage('题库覆盖读取失败：'+error.message,'error');
  }
}

async function loadExamChapters(){
  const sid=document.getElementById('e-subject').value;
  const div=document.getElementById('e-chapters');
  examPage.state.selectedExamChapters.clear();
  availableExamChapterIds.clear();
  resetExamBankCoverage(sid?'正在读取有效题库范围...':'选择科目后显示有效题库覆盖情况',sid?'loading':'idle');
  const revision=++examChapterRequestRevision;
  if(examChapterRequestController)examChapterRequestController.abort();
  div.replaceChildren();
  if(!sid){
    div.textContent='请先选择科目';
    return;
  }
  examChapterRequestController=new AbortController();
  div.setAttribute('aria-busy','true');
  div.textContent='正在读取章节...';
  try{
    const [chs,subjectCoverage]=await Promise.all([
      examPage.api.request(
        '/api/chapters?'+new URLSearchParams({subject_id:sid}).toString(),
        {signal:examChapterRequestController.signal}
      ),
      examPage.api.request(
        '/api/v2/question-bank/coverage?'+new URLSearchParams({subject_id:sid}).toString(),
        {signal:examChapterRequestController.signal}
      )
    ]);
    if(
      revision!==examChapterRequestRevision||
      document.getElementById('e-subject').value!==sid
    )return;
    const coveredChapterIds=new Set(
      (Array.isArray(subjectCoverage?.chapters)?subjectCoverage.chapters:[])
        .filter(chapter=>safeInteger(chapter?.total_questions)>0)
        .map(chapter=>safeInteger(chapter?.chapter_id))
        .filter(Boolean)
    );
    const availableChapters=Array.isArray(chs)
      ?chs.filter(chapter=>
        !isExcludedExamChapter(chapter)&&
        coveredChapterIds.has(safeInteger(chapter.id))
      )
      :[];
    if(!availableChapters.length){
      div.textContent='该科目暂无可用于组卷的章节';
      resetExamBankCoverage('该科目没有通过有效范围校验的题目','error');
      return;
    }
    availableChapters.forEach(chapter=>{
      const id=safeInteger(chapter.id);
      if(id)availableExamChapterIds.add(id);
    });
    div.innerHTML='<div class="chip-wrap">'+availableChapters.map(chapter=>{
      const id=safeInteger(chapter.id);
      return `<button type="button" class="chip js-exam-chapter" data-id="${id}" aria-pressed="false">${escapeHTML(chapter.name||'')}</button>`;
    }).join('')+'</div>';
    div.querySelectorAll('.js-exam-chapter').forEach(chip=>{
      chip.addEventListener('click',()=>pickChapter(chip,safeInteger(chip.dataset.id)));
    });
    renderExamBankCoverage(subjectCoverage);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examChapterRequestRevision)return;
    div.textContent='章节读取失败：'+error.message;
    resetExamBankCoverage('题库范围读取失败：'+error.message,'error');
  }finally{
    if(revision===examChapterRequestRevision)div.removeAttribute('aria-busy');
  }
}

function pickChapter(el,id){
  if(!id)return;
  el.classList.toggle('on');
  el.setAttribute('aria-pressed',String(el.classList.contains('on')));
  if(el.classList.contains('on'))examPage.state.selectedExamChapters.add(id);
  else examPage.state.selectedExamChapters.delete(id);
  void loadExamBankCoverage();
}

let pastExamSourcesLoaded=false;

function switchExamMode(mode){
  examPage.state.examMode=mode;
  ['free','mistakes','past'].forEach(name=>{
    const active=name===mode;
    const panel=document.getElementById('exam-'+name);
    const tab=document.getElementById('tab-'+name);
    panel.hidden=!active;
    tab.classList.toggle('on',active);
    tab.setAttribute('aria-selected',String(active));
    tab.tabIndex=active?0:-1;
  });
  if(mode==='free'){loadExamSubjects()}
  if(mode==='mistakes'){void prepareExamMistakeFilters()}
  if(mode==='past'){loadPastExamSources()}
  examPage.controller.setGenerateButton();
}

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
      card.innerHTML=`<div class="past-source-error">${icon('circleHelp')} ${escapeHTML(data?.error||'真题结构读取失败')}</div>`;
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
    card.innerHTML=`<div class="past-source-error">${icon('circleHelp')} 真题结构读取失败，请检查本地 TeX 目录。</div>`;
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
      ?`<div class="past-source-error">${icon('circleHelp')}<span>${escapeHTML(diagnostics.join('；'))}</span></div>`
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
    ${structure.year_selection_reason?`<div class="past-source-warning">${icon('circleHelp')}<span>${escapeHTML(structure.year_selection_reason)}</span></div>`:''}
    ${warnings.length?`<div class="past-source-warning">${icon('circleHelp')}<span>${escapeHTML(warnings.join('；'))}</span></div>`:''}
    <div class="past-source-rule">${icon('shieldCheck')}候选题源状态：${modeInfo&&modeInfo.available===false
      ?`不可用：${escapeHTML(String(modeInfo.reason||'该题源方式当前不可用'))}`
      :escapeHTML(String(modeInfo.acceptance_status||modeInfo.status||'not_run'))}</div>
    <div class="past-section-list">${sections||'<span class="past-section-empty">该年份未提供板块明细</span>'}</div>
    <div class="past-source-years">可选结构年份：${escapeHTML(usableStructures.map(item=>item.year).join('、'))}</div>`;
}

let examMistakes=[];

let examMistakeNextCursor=null;

let examMistakeTotal=0;

let examMistakeHasMore=false;

let examMistakeRequestRevision=0;

let examMistakeRequestController=null;

let examMistakeFiltersLoaded=false;

let examMistakeChapterRequestRevision=0;

let examMistakeChapterRequestController=null;

let examDailyRecommendations=[];

let examDailyRecommendationRevision=0;

let examDailyRecommendationController=null;

function resetExamMistakeCandidates(message='请先选择科目'){
  if(examMistakeRequestController)examMistakeRequestController.abort();
  examMistakeRequestRevision+=1;
  examMistakes=[];
  examMistakeNextCursor=null;
  examMistakeTotal=0;
  examMistakeHasMore=false;
  examPage.state.selectedExamMistakeIds.clear();
  const div=document.getElementById('exam-mistake-list');
  if(div)div.innerHTML=emptyStateHTML('错题候选暂不可用',{icon:'circleHelp',description:message,compact:true});
  updateExamMistakeSelection();
}

function renderExamDailyRecommendations(){
  const root=document.getElementById('exam-daily-recommendations');
  if(!root)return;
  if(!examDailyRecommendations.length){
    root.innerHTML=emptyStateHTML('暂无再次答错推荐',{
      icon:'check',description:'每日自主复习中记录“再次答错”后，活动且未掌握的题目会出现在这里。',compact:true
    });
    return;
  }
  root.innerHTML=examDailyRecommendations.map(item=>{
    const id=safeInteger(item.id);
    const selected=examPage.state.selectedExamMistakeIds.has(id);
    return `<article class="exam-daily-recommendation" data-recommendation-id="${id}">
      <div class="exam-daily-recommendation-main">
        <div class="exam-daily-recommendation-meta"><strong>${escapeHTML(item.subject||'其他')}</strong><span>${escapeHTML(item.chapter||'未分章节')}</span><span>再次答错 ${safeInteger(item.again_count)} 次</span></div>
        ${problemPreviewHTML(item.content,'exam-daily-recommendation-problem')}
      </div>
      <button type="button" class="btn ${selected?'btn-s':'btn-out'} btn-xs js-add-daily-recommendation" data-recommendation-id="${id}"${selected?' disabled aria-disabled="true"':''}>${icon(selected?'check':'plus')}${selected?'已加入':'加入'}</button>
    </article>`;
  }).join('');
  void renderMath(root);
}

async function loadExamDailyRecommendations(){
  const revision=++examDailyRecommendationRevision;
  examDailyRecommendationController?.abort();
  examDailyRecommendationController=new AbortController();
  const root=document.getElementById('exam-daily-recommendations');
  if(root)root.textContent='正在读取推荐…';
  const params=new URLSearchParams({limit:'10'});
  const subjectId=document.getElementById('em-subject')?.value||'';
  const chapterId=document.getElementById('em-chapter')?.value||'';
  if(subjectId)params.set('subject_id',subjectId);
  if(chapterId)params.set('chapter_id',chapterId);
  try{
    const page=await examPage.api.request('/api/v2/daily-review/recommendations?'+params.toString(),{
      signal:examDailyRecommendationController.signal
    });
    if(revision!==examDailyRecommendationRevision)return;
    const seen=new Set();
    examDailyRecommendations=(Array.isArray(page?.items)?page.items:[]).filter(item=>{
      const id=safeInteger(item?.id);
      if(!id||seen.has(id))return false;
      seen.add(id);return true;
    });
    renderExamDailyRecommendations();
  }catch(error){
    if(error?.name==='AbortError'||revision!==examDailyRecommendationRevision)return;
    if(root)root.textContent='推荐读取失败：'+error.message;
  }
}

async function addExamDailyRecommendation(id){
  const pageRevision=examPage.state.revision;
  let item=examDailyRecommendations.find(candidate=>safeInteger(candidate.id)===safeInteger(id));
  if(!item||examPage.state.selectedExamMistakeIds.has(safeInteger(id)))return;
  if(examPage.state.selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES){
    showModal('提示',`一次最多选择 ${MAX_EXAM_MISTAKE_REFERENCES} 道参考错题`,'info');
    return;
  }
  const subject=document.getElementById('em-subject');
  if(!subject?.value){
    subject.value=String(safeInteger(item.subject_id));
    syncCustomSelect(subject);
    await loadExamMistakeChapters();
    if(pageRevision!==examPage.state.revision)return;
    item=examDailyRecommendations.find(candidate=>safeInteger(candidate.id)===safeInteger(id))||item;
  }
  if(String(safeInteger(item.subject_id))!==String(subject.value)){
    showModal('科目不一致','请先切换到该推荐题所属科目。','info');
    return;
  }
  if(!examMistakes.some(candidate=>safeInteger(candidate.id)===safeInteger(item.id))){
    examMistakes.push(item);
  }
  examPage.state.selectedExamMistakeIds.add(safeInteger(item.id));
  updateExamMistakeSelection();
  renderExamDailyRecommendations();
}

async function prepareExamMistakeFilters(){
  const subjectSelect=document.getElementById('em-subject');
  if(!subjectSelect)return;
  if(!examMistakeFiltersLoaded){
    try{
      const subjects=await examPage.api.request('/api/subjects');
      subjectSelect.replaceChildren(new Option('请选择科目',''));
      subjects.forEach(subject=>subjectSelect.add(new Option(
        String(subject.name??''),
        String(safeInteger(subject.id,''))
      )));
      examMistakeFiltersLoaded=true;
    }catch(error){
    if(error?.name==='AbortError')return;
      resetExamMistakeCandidates('科目读取失败：'+error.message);
      return;
    }
  }
  if(subjectSelect.value)await loadExamMistakeChapters();
  else{
    resetExamMistakeCandidates();
    await loadExamDailyRecommendations();
  }
}

async function loadExamMistakeChapters(){
  const subjectSelect=document.getElementById('em-subject');
  const chapterSelect=document.getElementById('em-chapter');
  const sid=subjectSelect.value;
  const revision=++examMistakeChapterRequestRevision;
  if(examMistakeChapterRequestController)examMistakeChapterRequestController.abort();
  chapterSelect.replaceChildren(new Option(sid?'全部章节':'请先选择科目',''));
  resetExamMistakeCandidates(sid?'正在读取候选错题...':'请先选择科目');
  if(!sid)return;
  examMistakeChapterRequestController=new AbortController();
  try{
    const chapters=await examPage.api.request(
      '/api/chapters?'+new URLSearchParams({subject_id:sid}).toString(),
      {signal:examMistakeChapterRequestController.signal}
    );
    if(
      revision!==examMistakeChapterRequestRevision||
      subjectSelect.value!==sid
    )return;
    chapters.forEach(chapter=>chapterSelect.add(new Option(
      String(chapter.name??''),
      String(safeInteger(chapter.id,''))
    )));
    await Promise.all([loadExamMistakes(),loadExamDailyRecommendations()]);
  }catch(error){
    if(error?.name==='AbortError'||revision!==examMistakeChapterRequestRevision)return;
    resetExamMistakeCandidates('筛选读取失败：'+error.message);
  }
}

function filterExamMistakesByChapter(){
  examPage.state.selectedExamMistakeIds.clear();
  void Promise.all([loadExamMistakes(),loadExamDailyRecommendations()]);
}

function examMistakeIdentity(mistake){
  const date=String(mistake.date_added||'').trim()||'日期未记录';
  const qnum=String(mistake.qnum||'').trim();
  if(qnum)return `${date} · 题号 ${qnum}`;
  const preview=cleanProblemContent(mistake.content)
    .replace(/\\([a-zA-Z]+)\s*/g,'$1 ')
    .replace(/\\/g,'')
    .replace(/[{}$]/g,'')
    .replace(/\s+/g,' ')
    .trim();
  return `${date} · ${preview?`题目 ${preview.slice(0,18)}${preview.length>18?'…':''}`:'未填写题号'}`;
}

function updateExamMistakeSelection(){
  const count=document.getElementById('exam-selected-count');
  const summary=document.getElementById('exam-selection-summary');
  if(count)count.textContent=`已选 ${examPage.state.selectedExamMistakeIds.size} / 共 ${examMistakeTotal} 道（最多 ${MAX_EXAM_MISTAKE_REFERENCES} 道）`;
  if(!summary)return;

  const selected=examMistakes.filter(m=>examPage.state.selectedExamMistakeIds.has(Number(m.id)));
  if(!selected.length){
    summary.innerHTML=`<span class="exam-selection-summary-icon">${icon('circleHelp')}</span><span>勾选错题后，这里会显示所选题目的日期和题号。</span>`;
    summary.classList.remove('has-selection');
    return;
  }

  const visible=selected.slice(0,4)
    .map(m=>`<span class="exam-selected-token">${escapeHTML(examMistakeIdentity(m))}</span>`)
    .join('');
  const remaining=selected.length>4?`<span class="exam-selected-more">另有 ${selected.length-4} 道</span>`:'';
  summary.innerHTML=`<span class="exam-selection-summary-icon">${icon('check')}</span><span class="exam-selection-label">已选错题</span><span class="exam-selected-tokens">${visible}${remaining}</span>`;
  summary.classList.add('has-selection');
}

async function loadExamMistakes(options={}){
  const append=Boolean(options.append);
  const subjectId=document.getElementById('em-subject')?.value||'';
  const chapterId=document.getElementById('em-chapter')?.value||'';
  if(!subjectId){
    resetExamMistakeCandidates();
    return;
  }
  if(append&&(!examMistakeHasMore||!examMistakeNextCursor))return;
  const revision=++examMistakeRequestRevision;
  if(examMistakeRequestController)examMistakeRequestController.abort();
  examMistakeRequestController=new AbortController();
  if(!append){examMistakeNextCursor=null;examMistakeHasMore=false}
  const div=document.getElementById('exam-mistake-list');
  let page,data;
  try{
    const params=new URLSearchParams({limit:'30',subject_id:subjectId});
    if(chapterId)params.set('chapter_id',chapterId);
    if(append&&examMistakeNextCursor)params.set('after',examMistakeNextCursor);
    page=await examPage.api.request('/api/v2/exam-candidates?'+params.toString(),{signal:examMistakeRequestController.signal});
    if(revision!==examMistakeRequestRevision)return;
    data=Array.isArray(page?.items)?page.items:[];
  }catch(error){
    if(error?.name==='AbortError'||revision!==examMistakeRequestRevision)return;
    div.textContent='错题读取失败：'+error.message;
    return;
  }
  if(!Array.isArray(data))data=[];
  examMistakes=append?[...examMistakes,...data]:data;
  examMistakeNextCursor=page?.next_cursor||null;
  examMistakeHasMore=Boolean(page?.has_more&&examMistakeNextCursor);
  examMistakeTotal=Math.max(0,safeInteger(page?.total));
  if(!append)examPage.state.selectedExamMistakeIds.clear();
  data=examMistakes;
  updateExamMistakeSelection();
  if(!data.length){
    div.innerHTML=emptyStateHTML('没有可用的未掌握错题',{icon:'sparkles',description:'调整筛选条件，或先在错题列表中积累更多题目。',compact:true});
    return;
  }
  div.innerHTML=data.map(m=>{
    const date=String(m.date_added||'').trim()||'日期未记录';
    const qnum=String(m.qnum||'').trim();
    const subject=String(m.subject||'').trim()||'未分类科目';
    const chapter=String(m.chapter||'').trim()||'未分类章节';
    const reasons=String(m.reasons||'').split(',').map(r=>r.trim()).filter(Boolean);
    return `<label class="exam-mistake-option">
      <input class="exam-mistake-checkbox" type="checkbox" value="${safeInteger(m.id)}" onchange="toggleMistake(this)" aria-label="选择 ${escapeHTML(examMistakeIdentity(m))}" ${examPage.state.selectedExamMistakeIds.has(safeInteger(m.id))?'checked':''}>
      <span class="exam-mistake-content">
        <span class="exam-mistake-meta">
          <span class="exam-mistake-date">${icon('calendar')}${escapeHTML(date)}</span>
          <span class="exam-mistake-qnum">${qnum?`题号 ${escapeHTML(qnum)}`:'未填写题号'}</span>
          <span class="exam-mistake-location">${escapeHTML(subject)} · ${escapeHTML(chapter)}</span>
        </span>
        ${problemPreviewHTML(m.content,'exam-mistake-problem')}
        ${m.note?`<span class="exam-mistake-note">${icon('message')}${escapeHTML(m.note)}</span>`:''}
      </span>
      <span class="exam-mistake-reasons">${reasons.map(reason=>`<span class="badge badge-r">${escapeHTML(reason)}</span>`).join('')}</span>
    </label>`;
  }).join('')+(examMistakeHasMore?'<button type="button" class="btn btn-out btn-sm exam-candidates-more" id="exam-candidates-more">加载更多错题</button>':'')+`<div class="list-total">已显示 ${data.length} / ${examMistakeTotal} 道</div>`;
  document.getElementById('exam-candidates-more')?.addEventListener('click',()=>loadExamMistakes({append:true}));
  void renderMath(div);
}

function toggleMistake(cb){
  if(cb.checked){
    if(examPage.state.selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES){
      cb.checked=false;
      showModal('提示',`一次最多选择 ${MAX_EXAM_MISTAKE_REFERENCES} 道参考错题`,'info');
      return;
    }
    examPage.state.selectedExamMistakeIds.add(+cb.value);
  }else{
    examPage.state.selectedExamMistakeIds.delete(+cb.value);
  }
  updateExamMistakeSelection();renderExamDailyRecommendations();
}

function selectAllMistakes(){
  document.querySelectorAll('#exam-mistake-list input[type=checkbox]').forEach(c=>{
    if(examPage.state.selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES)return;
    c.checked=true;
    examPage.state.selectedExamMistakeIds.add(+c.value);
  });
  updateExamMistakeSelection();renderExamDailyRecommendations();
}

function selectNoMistakes(){
  document.querySelectorAll('#exam-mistake-list input[type=checkbox]').forEach(c=>c.checked=false);
  examPage.state.selectedExamMistakeIds.clear();
  updateExamMistakeSelection();renderExamDailyRecommendations();
}

function updateDifficultyStars(select){
  const target=document.getElementById(select.id+'-stars');
  if(!target)return;
  const levels={基础:1,中等:2,困难:3};
  const level=levels[select.value]||2;
  target.textContent='★'.repeat(level)+'☆'.repeat(3-level);
  target.setAttribute('aria-label',`难度 ${level} 星，共 3 星`);
}

let binding=null;
function bind(){
  if(binding)return;
  binding=new AbortController();
['e-diff','em-diff'].forEach(id=>{
  const select=document.getElementById(id);
  if(!select)return;
  select.addEventListener('change',()=>updateDifficultyStars(select),{signal:binding.signal});
  updateDifficultyStars(select);
});

document.getElementById('em-subject')?.addEventListener(
  'change',
  ()=>void loadExamMistakeChapters(),
  {signal:binding.signal});

document.getElementById('em-chapter')?.addEventListener(
  'change',
  filterExamMistakesByChapter,
  {signal:binding.signal});

document.getElementById('exam-daily-recommendations-refresh')?.addEventListener(
  'click',
  ()=>void loadExamDailyRecommendations(),
  {signal:binding.signal});

document.getElementById('exam-daily-recommendations')?.addEventListener('click',event=>{
  const button=event.target.closest('.js-add-daily-recommendation');
  if(button&&!button.disabled)void addExamDailyRecommendation(button.dataset.recommendationId);
},{signal:binding.signal});

document.querySelector('.tabs')?.addEventListener('keydown',event=>{
  const tabs=[...document.querySelectorAll('[role="tab"][data-exam-mode]')];
  const index=tabs.indexOf(document.activeElement);
  if(index<0)return;
  let next=index;
  if(event.key==='ArrowRight')next=(index+1)%tabs.length;
  else if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;
  else if(event.key==='Home')next=0;
  else if(event.key==='End')next=tabs.length-1;
  else return;
  event.preventDefault();
  tabs[next].focus();
  switchExamMode(tabs[next].dataset.examMode);
},{signal:binding.signal});
}
function unbind(){
  binding?.abort();binding=null;
  examSubjectRequestRevision+=1;
  examChapterRequestRevision+=1;
  examBankCoverageRequestRevision+=1;
  examMistakeRequestRevision+=1;
  examMistakeChapterRequestRevision+=1;
  examDailyRecommendationRevision+=1;
  [examChapterRequestController,examBankCoverageRequestController,
    examMistakeRequestController,examMistakeChapterRequestController,
    examDailyRecommendationController].forEach(controller=>controller?.abort());
  const subject=document.getElementById('e-subject');
  if(subject){delete subject.dataset.subjectLoading;subject.removeAttribute('aria-busy')}
}

let examSubjectRequestRevision=0;
async function loadExamSubjects(){
  const sel=document.getElementById('e-subject');
  if(!sel||sel.dataset.subjectLoading==='1')return;
  if(sel.options.length>1){
    if(document.getElementById('e-chapters')?.getAttribute('aria-busy')==='true'){
      await loadExamChapters();
    }else if(document.getElementById('exam-bank-coverage')?.classList.contains('is-loading')){
      await loadExamBankCoverage();
    }
    return;
  }
  const revision=++examSubjectRequestRevision;
  sel.dataset.subjectLoading='1';
  sel.replaceChildren(new Option('正在读取科目...',''));
  sel.disabled=true;
  sel.setAttribute('aria-busy','true');
  syncCustomSelect(sel);
  try{
    const data=await examPage.api.request('/api/subjects');
    if(!Array.isArray(data)||!data.length){
      sel.replaceChildren(new Option('暂无可选科目',''));
      syncCustomSelect(sel);
      return;
    }
    sel.replaceChildren(new Option('请选择',''));
    data.forEach(subject=>sel.add(new Option(
      String(subject.name??''),
      String(safeInteger(subject.id,''))
    )));
    sel.disabled=false;
    window.restoreRemoteDraftField?.(sel.id);
    syncCustomSelect(sel);
    sel.onchange=()=>loadExamChapters();
    await loadExamChapters();
  }catch(error){
    if(error?.name==='AbortError')return;
    console.error('loadSubjects error:',error);
    sel.replaceChildren(new Option('科目读取失败',''));
    sel.disabled=true;
    syncCustomSelect(sel);
    showModal('读取失败',error.message,'error');
  }finally{
    if(revision===examSubjectRequestRevision){
      delete sel.dataset.subjectLoading;
      sel.removeAttribute('aria-busy');
    }
  }
}


examPage.selection=Object.freeze({
  loadExamSubjects,
  isExcludedExamChapter,
  resetExamBankCoverage,
  renderExamBankCoverage,
  loadExamBankCoverage,
  loadExamChapters,
  pickChapter,
  switchExamMode,
  loadPastExamSources,
  loadPastExamSubjects,
  pastExamAvailabilityMessage,
  pastExamSourceUsable,
  pastExamYearStructures,
  updatePastCandidateNote,
  renderPastExamSource,
  resetExamMistakeCandidates,
  renderExamDailyRecommendations,
  loadExamDailyRecommendations,
  addExamDailyRecommendation,
  prepareExamMistakeFilters,
  loadExamMistakeChapters,
  filterExamMistakesByChapter,
  examMistakeIdentity,
  updateExamMistakeSelection,
  loadExamMistakes,
  toggleMistake,
  selectAllMistakes,
  selectNoMistakes,
  updateDifficultyStars,
  bind,
  unbind
});
})();
