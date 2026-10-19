(function initSharedUIRenderer(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.ui=window.Cuoti.ui||{};

  const EMPTY_STATE_ROUTE_PAGES=new Set(['add','list','input','review','exam','stats','settings']);
  const EMPTY_STATE_ICON_NAMES=new Set([
    'activity','archive','barsHorizontal','bookOpen','calendar','chart','circleAlert','circleCheck','circleHelp','examPaper','fileText','filter','history','lightbulb','notebook','pie','search','sparkles','trash','trendLine'
  ]);

  function escape(value){
    return String(value??'')
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/'/g,'&#39;');
  }

  function cleanProblemContent(value){
    return String(value??'')
      .replace(/\r\n?/g,'\n')
      .replace(/```(?:latex|tex)?\s*/gi,'')
      .replace(/```/g,'')
      .replace(/\\documentclass(?:\[[^\]]*\])?\{[^}]*\}/g,'')
      .replace(/\\usepackage(?:\[[^\]]*\])?\{[^}]*\}/g,'')
      .replace(/\\(?:begin|end)\{document\}/g,'')
      .replace(/\\(?:sub)*section\*?\{([^}]*)\}/g,'$1\n')
      .replace(/\\(?:textbf|textit|emph)\{([^}]*)\}/g,'$1')
      .replace(/\\begin\{proof\}/g,'证明：')
      .replace(/\\end\{proof\}/g,'')
      .replace(/\\(?:begin|end)\{(?:problem|solution|note|enumerate|itemize)\}/g,'')
      .replace(/\\item\s*/g,'• ')
      .replace(/\n{3,}/g,'\n\n')
      .trim();
  }

  function problemPreview(content,variant=''){
    const cleaned=cleanProblemContent(content);
    if(!cleaned)return '<span class="problem-empty">--</span>';
    return `<div class="math problem-preview ${variant}">${escape(cleaned).replace(/\n/g,'<br>')}</div>`;
  }

  function emptyState(title,options={}){
    const iconName=EMPTY_STATE_ICON_NAMES.has(options.icon)?options.icon:'circleHelp';
    const classes=['empty-state'];
    if(options.chart)classes.push('chart-empty');
    if(options.compact)classes.push('compact');
    const description=options.description
      ?`<span>${escape(options.description)}</span>`
      :'';
    const action=options.actionLabel&&EMPTY_STATE_ROUTE_PAGES.has(options.page)
      ?`<button type="button" class="btn btn-p btn-sm" data-action="navigate" data-page="${options.page}">${escape(options.actionLabel)}</button>`
      :(options.actionLabel&&/^[a-z-]+$/.test(String(options.emptyAction||''))
        ?`<button type="button" class="btn btn-out btn-sm" data-empty-action="${options.emptyAction}">${escape(options.actionLabel)}</button>`
        :'');
    return `<div class="${classes.join(' ')}"><div class="empty-state-icon">${icon(iconName)}</div><div class="empty-state-copy"><strong>${escape(title)}</strong>${description}</div>${action}</div>`;
  }

  window.Cuoti.ui.render=Object.freeze({
    escape,
    cleanProblemContent,
    problemPreview,
    emptyState
  });
})();
