(function initMistakeRenderer(){
  'use strict';

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.mistakes=window.Cuoti.mistakes||{};

  const PREVIEW_LIMIT=300;

  const MATH_ENVIRONMENTS=new Set([
    'math','displaymath','equation','equation*','align','align*','gather',
    'gather*','multline','multline*','split','cases','matrix','pmatrix',
    'bmatrix','vmatrix','Vmatrix'
  ]);

  function escapedAt(text,index){
    let slashes=0;
    for(let cursor=index-1;cursor>=0&&text[cursor]==='\\';cursor--)slashes++;
    return slashes%2===1;
  }

  function closingAt(text,start,open,close){
    let cursor=start+open.length;
    while(cursor<text.length){
      const found=text.indexOf(close,cursor);
      if(found<0)return -1;
      if(!escapedAt(text,found))return found+close.length;
      cursor=found+close.length;
    }
    return -1;
  }

  function mathTokenAt(text,start){
    const delimiters=[['\\[','\\]'],['\\(','\\)'],['$$','$$'],['$','$']];
    for(const [open,close] of delimiters){
      if(!text.startsWith(open,start)||escapedAt(text,start))continue;
      const end=closingAt(text,start,open,close);
      return {end:end<0?text.length:end,unclosed:end<0};
    }
    const environment=/^\\begin\{([^}]+)\}/.exec(text.slice(start));
    if(environment&&MATH_ENVIRONMENTS.has(environment[1])){
      const close=`\\end{${environment[1]}}`;
      const end=closingAt(text,start,environment[0],close);
      return {end:end<0?text.length:end,unclosed:end<0};
    }
    return null;
  }

  function tokenize(value){
    const text=String(value??''),tokens=[];
    let cursor=0,textStart=0;
    while(cursor<text.length){
      const math=mathTokenAt(text,cursor);
      if(!math){cursor++;continue}
      if(cursor>textStart)tokens.push({math:false,value:text.slice(textStart,cursor)});
      tokens.push({math:true,value:text.slice(cursor,math.end),unclosed:math.unclosed});
      cursor=math.end;textStart=cursor;
    }
    if(textStart<text.length)tokens.push({math:false,value:text.slice(textStart)});
    return tokens;
  }

  function plainTruncate(text,limit){
    const value=String(text??'');
    if(Array.from(value).length<=limit)return value;
    const lines=value.split(/\r?\n/),kept=[];
    let length=0;
    for(const line of lines){
      const added=(kept.length?1:0)+Array.from(line).length;
      if(kept.length&&length+added>limit)break;
      kept.push(line);length+=added;
      if(length>=limit)break;
    }
    const preview=kept.join('\n')||Array.from(lines[0]||'').slice(0,limit).join('');
    return `${preview}\n…（已截断）`;
  }

  function truncate(value,limit=PREVIEW_LIMIT){
    const text=String(value??'');
    if(Array.from(text).length<=limit)return text;
    const tokens=tokenize(text);
    if(!tokens.some(token=>token.math))return plainTruncate(text,limit);
    if(tokens.some(token=>token.unclosed)){
      // A damaged source cannot be safely rendered as TeX.  Remove only the
      // opening markers and show a plain-text excerpt instead.
      return plainTruncate(
        text.replace(/\\\[|\\\(|\$\$?|\\begin\{(?:math|displaymath|equation\*?|align\*?)\}/g,''),
        limit
      );
    }
    let used=0,preview='';
    for(const token of tokens){
      const length=Array.from(token.value).length;
      if(token.math){
        // Keep formula tokens whole.  A single long formula is preferable to
        // an invalid delimiter; CSS still controls the card's visual height.
        if(!preview||used+length<=limit){preview+=token.value;used+=length;continue}
        break;
      }
      const remaining=limit-used;
      if(remaining<=0)break;
      const chars=Array.from(token.value);
      if(chars.length<=remaining){preview+=token.value;used+=chars.length;continue}
      preview+=chars.slice(0,remaining).join('');break;
    }
    return `${preview.trimEnd()}\n…（已截断）`;
  }

  function reasons(value){
    const values=Array.isArray(value)?value:String(value||'').split(',');
    return values.map(reason=>String(reason).trim()).filter(Boolean);
  }

  function groupByDateAndSubject(data){
    const groups=Object.create(null);
    data.forEach(item=>{
      const date=item.date_added||'未知日期';
      if(!groups[date])groups[date]=Object.create(null);
      const subject=item.subject||'其他';
      if(!groups[date][subject])groups[date][subject]=[];
      groups[date][subject].push(item);
    });
    return groups;
  }

  function card(item,{selectedPublicIds,reasonOptions}){
    const done=item.status==='已掌握';
    const mistakeId=safeInteger(item.id);
    const selected=selectedPublicIds.has(String(item.public_id||''));
    const itemReasons=reasons(item.reasons);
    const reasonSet=new Set(itemReasons);
    return `<div class="mistake-card${done?' done':''}${selected?' selected':''}" data-mistake-id="${mistakeId}">
      <div class="mc-header">
        <label class="mistake-select-control" title="选择此错题">
          <input type="checkbox" class="js-select-mistake" data-id="${mistakeId}" aria-label="选择错题 ${escapeHTML(item.qnum||String(mistakeId))}"${selected?' checked':''}>
        </label>
        <button type="button" class="mc-chapter js-chapter-toggle" data-id="${mistakeId}" aria-expanded="false" aria-controls="mistake-chapter-editor-${mistakeId}" title="修改章节">
          <span class="mc-chapter-label">${escapeHTML(item.chapter||'未归类')}</span>${icon('edit','inline-icon')}
        </button>
        <span class="mc-qnum">${escapeHTML(item.qnum||'')}</span>
        <div class="mc-status">
          <span class="badge ${done?'badge-g':'badge-b'}">${escapeHTML(item.status||'')}</span>
          <button type="button" class="mc-reason-toggle js-reason-toggle" data-id="${mistakeId}" aria-expanded="false" aria-controls="mistake-reason-editor-${mistakeId}">错因${icon('chevronDown')}</button>
        </div>
      </div>
      <div class="mc-chapter-editor" id="mistake-chapter-editor-${mistakeId}" hidden>
        <label for="mistake-chapter-select-${mistakeId}">${icon('edit','inline-icon')}修改章节</label>
        <select id="mistake-chapter-select-${mistakeId}" class="mc-chapter-select" data-id="${mistakeId}" disabled>
          <option value="">正在读取章节...</option>
        </select>
        <span class="mc-chapter-save-status" role="status" aria-live="polite"></span>
        <div class="mc-chapter-editor-actions">
          <button type="button" class="btn btn-out btn-xs js-chapter-cancel" data-id="${mistakeId}">取消</button>
          <button type="button" class="btn btn-p btn-xs js-chapter-save" data-id="${mistakeId}" disabled>保存</button>
        </div>
      </div>
      <div class="mc-reason-editor" id="mistake-reason-editor-${mistakeId}" hidden>
        <div class="mc-reason-editor-heading">${icon('circleHelp','inline-icon')}选择错因 <span>点击后自动保存</span></div>
        <div class="mc-reason-options">
          ${reasonOptions.map(reason=>`<button type="button" class="mc-reason-choice js-reason-choice${reasonSet.has(reason)?' on':''}" data-id="${mistakeId}" data-reason="${escapeHTML(reason)}" aria-pressed="${reasonSet.has(reason)}">${escapeHTML(reason)}</button>`).join('')}
        </div>
        <div class="mc-reason-save-status" aria-live="polite"></div>
      </div>
      <div class="mc-content">${item.content?problemPreviewHTML(truncate(item.content),'problem-preview-card'):'<span class="mc-content-empty">--</span>'}</div>
      <div class="mc-note-editor" id="mistake-note-editor-${mistakeId}" hidden>
        <label for="mistake-note-input-${mistakeId}">${icon('edit','inline-icon')}备注</label>
        <textarea id="mistake-note-input-${mistakeId}" maxlength="20000" rows="4">${escapeHTML(item.note||'')}</textarea>
        <div class="mc-note-editor-footer">
          <span class="mc-note-count"><span>0</span> / 20000</span>
          <span class="mc-note-save-status" aria-live="polite"></span>
          <button type="button" class="btn btn-out btn-xs js-note-cancel" data-id="${mistakeId}">取消</button>
          <button type="button" class="btn btn-p btn-xs js-note-save" data-id="${mistakeId}">保存</button>
        </div>
        <div class="mc-note-conflict" hidden>
          <span>此题已在其他页面更新。</span>
          <button type="button" class="btn btn-out btn-xs js-note-load-latest" data-id="${mistakeId}">载入最新内容</button>
          <button type="button" class="btn btn-out btn-xs js-note-keep-draft" data-id="${mistakeId}">保留当前草稿</button>
        </div>
      </div>
      <div class="mc-footer">
        <span class="mc-reasons" id="mistake-reason-badges-${mistakeId}">${itemReasons.map(reason=>`<span class="badge badge-r">${escapeHTML(reason)}</span>`).join('')}</span>
        <span class="mc-note" id="mistake-note-display-${mistakeId}"${item.note?'':' hidden'}>${icon('message','inline-icon')}<span class="mc-note-text math">${escapeHTML(item.note||'')}</span></span>
        <span class="mc-actions">
          <button type="button" class="btn btn-out btn-xs js-attachments-toggle" data-id="${mistakeId}" aria-expanded="false" aria-controls="mistake-attachments-${mistakeId}" title="附件" aria-label="附件">${icon('paperclip')}</button>
          <button type="button" class="btn btn-out btn-xs js-open-daily-analysis" data-id="${mistakeId}" data-date="${escapeHTML(item.date_added||'')}" data-public-id="${escapeHTML(item.public_id||'')}" title="单题每日分析" aria-label="单题每日分析">${icon('sparkles')}</button>
          <button type="button" class="btn btn-out btn-xs js-note-toggle" data-id="${mistakeId}" aria-expanded="false" aria-controls="mistake-note-editor-${mistakeId}" title="${item.note?'编辑备注':'添加备注'}" aria-label="${item.note?'编辑备注':'添加备注'}">${icon('edit')}</button>
          <button class="btn btn-xs ${done?'btn-o':'btn-s'} js-toggle-mistake" data-id="${mistakeId}" title="${done?'重新练习':'标记掌握'}" aria-label="${done?'重新练习':'标记掌握'}">${icon(done?'undo':'check')}</button>
          <button class="btn btn-d btn-xs js-delete-mistake" data-id="${mistakeId}" title="删除" aria-label="删除">${icon('x')}</button>
        </span>
      </div>
      <div class="mc-attachments" id="mistake-attachments-${mistakeId}" hidden>
        <div class="mc-attachment-toolbar">
          <strong>图片附件</strong><span>JPEG、PNG 或 WebP，单个不超过 10 MB</span>
          <button type="button" class="btn btn-out btn-xs js-attachment-pick" data-id="${mistakeId}">${icon('upload')}上传</button>
          <input type="file" class="js-attachment-input" data-id="${mistakeId}" accept="image/jpeg,image/png,image/webp" hidden>
        </div>
        <div class="mc-attachment-status" role="status"></div>
        <div class="mc-attachment-list"></div>
      </div>
    </div>`;
  }

  function attachmentList(items){
    if(!items.length)return '<div class="mc-attachment-empty">暂无附件</div>';
    return items.map(item=>{
      const id=encodeURIComponent(String(item.public_id||''));
      const size=Math.max(0,Number(item.byte_size)||0);
      return `<a class="mc-attachment-item" href="/api/v2/attachments/${id}/content" target="_blank" rel="noopener">
        <img src="/api/v2/attachments/${id}/content" alt="错题附件预览" loading="lazy">
        <span>${escapeHTML(String(item.extension||'图片').toUpperCase())} · ${(size/1024).toFixed(1)} KB</span>
      </a>`;
    }).join('');
  }

  function trashCard(item){
    const id=safeInteger(item.id);
    return `<div class="mistake-card trash-card">
      <div class="mc-header"><span class="mc-chapter">${escapeHTML(item.subject||'其他')} · ${escapeHTML(item.chapter||'--')}</span><span class="mc-qnum">${escapeHTML(item.qnum||'')}</span></div>
      <div class="mc-content">${item.content?problemPreviewHTML(truncate(item.content),'problem-preview-card'):'--'}</div>
      <div class="mc-footer"><span class="mc-note">删除时间：${escapeHTML(item.deleted_at||'未知')}${item.has_daily_analysis?' · 每日分析已保留':''}</span><span class="mc-actions">
        <button type="button" class="btn btn-out btn-xs js-restore-mistake" data-id="${id}">${icon('undo')}恢复</button>
        <button type="button" class="btn btn-d btn-xs js-purge-mistake" data-id="${id}">${icon('trash')}永久删除</button>
      </span></div>
    </div>`;
  }

  function trashList(data){
    const items=Array.isArray(data)?data:[];
    if(!items.length)return emptyStateHTML('回收站为空',{icon:'trash',description:'移入回收站的错题会显示在这里。'});
    return `<div class="trash-list">${items.map(trashCard).join('')}</div>`;
  }

  function list(data,options={}){
    const items=Array.isArray(data)?data:[];
    if(!items.length){
      return emptyStateHTML('还没有错题记录',{
        icon:'bookOpen',description:'标记后会在这里按日期和科目归档。',
        actionLabel:'去标记错题',page:'add'
      });
    }
    const selectedPublicIds=options.selectedPublicIds&&typeof options.selectedPublicIds.has==='function'
      ?options.selectedPublicIds:new Set();
    const reasonOptions=Array.isArray(options.reasonOptions)?options.reasonOptions:[];
    const groups=groupByDateAndSubject(items);
    let html='';
    for(const date of Object.keys(groups).sort().reverse()){
      const dayMistakes=items.filter(item=>(item.date_added||'未知日期')===date);
      const unmastered=dayMistakes.filter(item=>item.status==='未掌握').length;
      html+=`<div class="date-group" data-date="${escapeHTML(date)}"><div class="date-header">
        <span class="date-label">${icon('calendar','inline-icon')}${escapeHTML(date)}</span>
        <span class="date-stat">${dayMistakes.length}题 · ${unmastered}未掌握</span>
        <button class="btn btn-xs btn-o js-analyze-day" data-date="${escapeHTML(date)}" title="AI分析当日错题">${icon('sparkles')}分析当日</button>
      </div>`;
      for(const subject of Object.keys(groups[date])){
        const subjectItems=groups[date][subject];
        html+=`<div class="subject-group" data-subject="${escapeHTML(subject)}"><div class="subject-header">${icon('bookOpen','inline-icon')}${escapeHTML(subject)} <span class="subject-count">${subjectItems.length}题</span></div>`;
        subjectItems.forEach(item=>{html+=card(item,{selectedPublicIds,reasonOptions})});
        html+='</div>';
      }
      html+='</div>';
    }
    return html;
  }

  window.Cuoti.mistakes.render=Object.freeze({
    truncate,tokenize,reasons,groupByDateAndSubject,card,list,attachmentList,trashCard,trashList
  });
})();
