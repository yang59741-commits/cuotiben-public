(function initLatexGuide(){
  'use strict';

  const categories=Object.freeze([
    '定界符与换行','上下标、分数与根式','极限、求和与积分','常用函数与希腊字母',
    '关系、集合与逻辑','括号、绝对值、范数与向量','矩阵与行列式',
    '分段函数、方程组与多行对齐','数学文字与空格','运算、箭头与特殊符号'
  ]);
  const catalog=Object.freeze([
    {id:'inline',category:categories[0],title:'行内公式',keywords:'行内 定界符 dollar',before:'\\(',after:'\\)',placeholder:'x^2+y^2=r^2'},
    {id:'display',category:categories[0],title:'独立公式',keywords:'独立 居中 换行',before:'\\[\n',after:'\n\\]',placeholder:'E=mc^2'},
    {id:'power',category:categories[1],title:'上下标',keywords:'指数 下标',before:'\\(',after:'\\)',placeholder:'a_{n+1}=x^{n+1}'},
    {id:'fraction-root',category:categories[1],title:'分数与根式',keywords:'分数 根号 开方',before:'\\(',after:'\\)',placeholder:'\\frac{a}{b}+\\sqrt[n]{x}'},
    {id:'limit',category:categories[2],title:'极限',keywords:'趋于 无穷',before:'\\(',after:'\\)',placeholder:'\\lim_{x\\to0}\\frac{\\sin x}{x}=1'},
    {id:'sum-integral',category:categories[2],title:'求和、乘积与积分',keywords:'sigma product 积分',before:'\\[',after:'\\]',placeholder:'\\sum_{k=1}^{n}k+\\prod_{i=1}^{m}a_i+\\int_a^b f(x)\\,dx'},
    {id:'functions',category:categories[3],title:'常用函数',keywords:'正弦 余弦 对数 指数',before:'\\(',after:'\\)',placeholder:'\\sin x+\\cos x+\\ln x+\\exp x'},
    {id:'greek',category:categories[3],title:'希腊字母',keywords:'希腊 字母 小写 大写 变体 阿尔法 贝塔 伽马 德尔塔 伊普西龙 西塔 拉姆达 派 欧米伽',choices:['\\alpha','\\beta','\\gamma','\\delta','\\epsilon','\\varepsilon','\\zeta','\\eta','\\theta','\\vartheta','\\iota','\\kappa','\\lambda','\\mu','\\nu','\\xi','o','\\pi','\\varpi','\\rho','\\varrho','\\sigma','\\varsigma','\\tau','\\upsilon','\\phi','\\varphi','\\chi','\\psi','\\omega','\\Gamma','\\Delta','\\Theta','\\Lambda','\\Xi','\\Pi','\\Sigma','\\Upsilon','\\Phi','\\Psi','\\Omega']},
    {id:'relations',category:categories[4],title:'关系与逻辑',keywords:'小于 大于 不等 推出 当且仅当',before:'\\(',after:'\\)',placeholder:'a\\le b,\\ x\\ne0,\\ P\\Rightarrow Q,\\ P\\Leftrightarrow Q'},
    {id:'sets',category:categories[4],title:'集合',keywords:'属于 实数 自然数 并集 交集',before:'\\(',after:'\\)',placeholder:'x\\in\\mathbb{R},\\ A\\cup B,\\ A\\cap B,\\ A\\subseteq B'},
    {id:'brackets',category:categories[5],title:'自动伸缩括号',keywords:'圆括号 方括号 大括号',before:'\\(',after:'\\)',placeholder:'\\left(\\frac{a}{b}\\right),\\ \\left\\{x\\mid x>0\\right\\}'},
    {id:'norm-vector',category:categories[5],title:'绝对值、范数与向量',keywords:'模 长度 向量 粗体',before:'\\(',after:'\\)',placeholder:'|x|,\\ \\lVert v\\rVert,\\ \\vec{a},\\ \\boldsymbol{x}'},
    {id:'matrix',category:categories[6],title:'矩阵',keywords:'pmatrix 方阵',before:'\\[\n\\begin{pmatrix}\n',after:'\n\\end{pmatrix}\n\\]',placeholder:'1 & 2 \\\\\n3 & 4',matrix:true},
    {id:'determinant',category:categories[6],title:'行列式',keywords:'det determinant vmatrix',before:'\\[\n\\begin{vmatrix}\n',after:'\n\\end{vmatrix}\n\\]',placeholder:'a & b \\\\\nc & d'},
    {id:'cases',category:categories[7],title:'分段函数',keywords:'cases 分情况',before:'\\[f(x)=\\begin{cases}\n',after:'\n\\end{cases}\\]',placeholder:'x^2,&x\\ge0\\\\\n-x,&x<0'},
    {id:'aligned',category:categories[7],title:'多行对齐',keywords:'aligned 等号 对齐',before:'\\[\\begin{aligned}\n',after:'\n\\end{aligned}\\]',placeholder:'a&=b+c\\\\\n &=d+e'},
    {id:'system',category:categories[7],title:'方程组',keywords:'system 方程组',before:'\\[\\begin{cases}\n',after:'\n\\end{cases}\\]',placeholder:'x+y=1\\\\\nx-y=0'},
    {id:'text-space',category:categories[8],title:'公式内文字与空格',keywords:'text quad 空白 中文',before:'\\(',after:'\\)',placeholder:'x>0\\quad\\text{时函数递增}'},
    {id:'operators',category:categories[9],title:'运算符号',keywords:'乘号 点乘 正负 除号 不等 约等于 比例',choices:['\\times','\\cdot','\\pm','\\mp','\\div','\\neq','\\approx','\\propto']},
    {id:'arrows-logic',category:categories[9],title:'箭头与逻辑',keywords:'箭头 映射 推出 当且仅当 任意 存在 因此 因为',choices:['\\to','\\mapsto','\\leftrightarrow','\\Rightarrow','\\Leftrightarrow','\\forall','\\exists','\\therefore','\\because']},
  ]);
  const pinned={
    inline:{before:'\\(',after:'\\)',placeholder:"f'(x)=2x"},
    limit:{before:'\\[',after:'\\]',placeholder:'\\lim_{x\\to0}\\frac{\\sin x}{x}=1'},
  };
  const matrixState={rows:[['1','2'],['3','4']]};
  const choiceState={greek:'\\alpha',operators:'\\times','arrows-logic':'\\to'};
  const choiceGlyphs=Object.freeze({
    '\\alpha':'α','\\beta':'β','\\gamma':'γ','\\delta':'δ','\\epsilon':'ϵ','\\varepsilon':'ε',
    '\\zeta':'ζ','\\eta':'η','\\theta':'θ','\\vartheta':'ϑ','\\iota':'ι','\\kappa':'κ',
    '\\lambda':'λ','\\mu':'μ','\\nu':'ν','\\xi':'ξ','o':'ο','\\pi':'π','\\varpi':'ϖ',
    '\\rho':'ρ','\\varrho':'ϱ','\\sigma':'σ','\\varsigma':'ς','\\tau':'τ','\\upsilon':'υ',
    '\\phi':'ϕ','\\varphi':'φ','\\chi':'χ','\\psi':'ψ','\\omega':'ω',
    '\\Gamma':'Γ','\\Delta':'Δ','\\Theta':'Θ','\\Lambda':'Λ','\\Xi':'Ξ','\\Pi':'Π',
    '\\Sigma':'Σ','\\Upsilon':'Υ','\\Phi':'Φ','\\Psi':'Ψ','\\Omega':'Ω',
    '\\times':'×','\\cdot':'·','\\pm':'±','\\mp':'∓','\\div':'÷','\\neq':'≠',
    '\\approx':'≈','\\propto':'∝','\\to':'→','\\mapsto':'↦','\\leftrightarrow':'↔',
    '\\Rightarrow':'⇒','\\Leftrightarrow':'⇔','\\forall':'∀','\\exists':'∃',
    '\\therefore':'∴','\\because':'∵'
  });
  const state={category:'',targetId:'s-problem'};

  function serializeMatrix(rows=matrixState.rows){return rows.map(row=>row.map(value=>String(value??'')).join(' & ')).join(' \\\\\n')}
  function source(entry){if(entry.matrix)return entry.before+serializeMatrix()+entry.after;if(entry.choices)return '\\('+String(choiceState[entry.id]||entry.choices[0])+'\\)';return entry.before+entry.placeholder+entry.after}
  function matrixEntry(){return catalog.find(entry=>entry.id==='matrix')}
  function pinnedSource(id){const entry=pinned[id];return entry?entry.before+entry.placeholder+entry.after:''}

  function updateTargetLabel(){
    const label=document.getElementById('latex-guide-target');
    if(label)label.textContent=state.targetId==='s-note'?'插入到：备注':'插入到：题目';
    document.querySelectorAll('.latex-target-choice').forEach(button=>{
      const active=button.dataset.target===state.targetId;
      button.classList.toggle('on',active);button.setAttribute('aria-pressed',String(active));
    });
  }

  function filtered(){
    const query=String(document.getElementById('latex-guide-search')?.value||'').trim().toLowerCase();
    return catalog.filter(entry=>(!state.category||entry.category===state.category)&&(
      !query||`${entry.title} ${entry.keywords} ${source(entry)} ${(entry.choices||[]).join(' ')}`.toLowerCase().includes(query)
    ));
  }

  // Keep the wide interactive cards together at the end so the two-column
  // catalog can fill every earlier row without leaving a trailing blank cell.
  function isWideEntry(entry){return Boolean(entry.matrix||entry.id==='greek'||entry.id==='operators'||entry.id==='arrows-logic')}

  function render(){
    const root=document.getElementById('latex-guide-results');
    if(!root)return;
    const entries=filtered().sort((left,right)=>Number(isWideEntry(left))-Number(isWideEntry(right)));
    if(!entries.length){
      root.innerHTML=emptyStateHTML('没有匹配的 LaTeX 写法',{icon:'search',description:'请更换关键词或选择“全部”。',compact:true});
      return;
    }
    root.innerHTML=entries.map(entry=>entry.matrix?matrixSnippetHTML(entry):entry.choices?choiceSnippetHTML(entry):`<article class="latex-snippet" data-snippet-id="${escapeHTML(entry.id)}">
      <header><div><strong>${escapeHTML(entry.title)}</strong><span>${escapeHTML(entry.category)}</span></div><div class="latex-snippet-actions"><button type="button" class="btn btn-out btn-xs js-latex-copy" data-snippet-id="${escapeHTML(entry.id)}">${icon('clipboard')}复制</button><button type="button" class="btn btn-p btn-xs js-latex-insert" data-snippet-id="${escapeHTML(entry.id)}">${icon('plus')}插入</button></div></header>
      <div class="latex-cheatsheet-row"><code class="latex-source no-math">${escapeHTML(source(entry))}</code><div class="latex-result math">${escapeHTML(source(entry))}</div></div>
    </article>`).join('');
    void renderMath(root);
  }

  function choiceSnippetHTML(entry){
    const options=entry.choices.map(choice=>`<button type="button" class="latex-choice${choiceState[entry.id]===choice?' on':''}" data-choice-entry="${escapeHTML(entry.id)}" data-choice-value="${escapeHTML(choice)}" aria-label="${escapeHTML(choice)}" title="${escapeHTML(choice)}" aria-pressed="${choiceState[entry.id]===choice}"><span class="latex-choice-symbol no-math">${escapeHTML(choiceGlyphs[choice]||choice)}</span></button>`).join('');
    return `<article class="latex-snippet" data-snippet-id="${escapeHTML(entry.id)}"><header><div><strong>${escapeHTML(entry.title)}</strong><span>${escapeHTML(entry.category)}</span></div><div class="latex-snippet-actions"><button type="button" class="btn btn-out btn-xs js-latex-copy" data-snippet-id="${escapeHTML(entry.id)}">${icon('clipboard')}复制</button><button type="button" class="btn btn-p btn-xs js-latex-insert" data-snippet-id="${escapeHTML(entry.id)}">${icon('plus')}插入</button></div></header><div class="latex-choice-grid">${options}</div><div class="latex-cheatsheet-row"><code class="latex-source no-math js-choice-source">${escapeHTML(source(entry))}</code><div class="latex-result math js-choice-result">${escapeHTML(source(entry))}</div></div></article>`;
  }

  function selectChoice(id,value,article){
    const entry=entryById(id);
    if(!entry?.choices?.includes(value))return false;
    choiceState[id]=value;
    if(article){
      article.querySelectorAll('.latex-choice').forEach(button=>{
        const active=button.dataset.choiceValue===value;
        button.classList.toggle('on',active);button.setAttribute('aria-pressed',String(active));
      });
      article.querySelector('.js-choice-source').textContent=source(entry);
      const preview=article.querySelector('.js-choice-result');
      preview.textContent=source(entry);
      void renderMath(preview);
    }
    return true;
  }

  function matrixSnippetHTML(entry){
    const rows=matrixState.rows;
    const controls=(axis,index,count)=>{
      const unit=axis==='row'?'行':'列';
      return `<div class="latex-matrix-actions"><button type="button" class="latex-matrix-control" data-matrix-action="add-${axis}" data-matrix-index="${index}" aria-label="在第 ${index+1} ${unit}后增加一${unit}" title="在第 ${index+1} ${unit}后增加一${unit}" ${count>=8?'disabled':''}>${icon('plus')}</button><button type="button" class="latex-matrix-control" data-matrix-action="remove-${axis}" data-matrix-index="${index}" aria-label="删除第 ${index+1} ${unit}" title="删除第 ${index+1} ${unit}" ${count<=1?'disabled':''}>${icon('trash')}</button></div>`;
    };
    const columns=rows[0].map((_,index)=>`<th scope="col">${controls('col',index,rows[0].length)}</th>`).join('');
    const cells=rows.map((row,rowIndex)=>`<tr>${row.map((value,colIndex)=>`<td><input class="latex-matrix-cell" type="text" value="${escapeHTML(value)}" data-matrix-row="${rowIndex}" data-matrix-col="${colIndex}" aria-label="第 ${rowIndex+1} 行第 ${colIndex+1} 列" autocomplete="off" spellcheck="false"></td>`).join('')}<td>${controls('row',rowIndex,rows.length)}</td></tr>`).join('');
    return `<article class="latex-snippet latex-matrix-snippet" data-snippet-id="${escapeHTML(entry.id)}"><header><div><strong>${escapeHTML(entry.title)}</strong><span>${rows.length} × ${rows[0].length}</span></div><div class="latex-snippet-actions"><button type="button" class="btn btn-out btn-xs js-latex-copy" data-snippet-id="matrix">${icon('clipboard')}复制</button><button type="button" class="btn btn-p btn-xs js-latex-insert" data-snippet-id="matrix">${icon('plus')}插入</button></div></header><div class="latex-matrix-editor"><table class="latex-matrix-grid" aria-label="矩阵元素"><thead><tr>${columns}<td></td></tr></thead><tbody>${cells}</tbody></table></div><div class="latex-cheatsheet-row"><code class="latex-source no-math js-matrix-source">${escapeHTML(source(entry))}</code><div class="latex-result math js-matrix-result">${escapeHTML(source(entry))}</div></div></article>`;
  }

  function updateMatrixPreview(){
    const entry=matrixEntry();if(!entry)return;
    const value=source(entry);
    document.querySelectorAll('.js-matrix-source').forEach(node=>{node.textContent=value;});
    document.querySelectorAll('.js-matrix-result').forEach(node=>{node.textContent=value;});
    void renderMath(document.querySelector('.latex-matrix-snippet'));
  }

  function matrixAction(action,index){
    const rows=matrixState.rows;const cols=rows[0]?.length||1;
    const size=action.endsWith('-row')?rows.length:cols;
    if(!Number.isInteger(index)||index<0||index>=size)return false;
    if(action==='add-row'&&rows.length<8)rows.splice(index+1,0,Array(cols).fill(''));
    else if(action==='remove-row'&&rows.length>1)rows.splice(index,1);
    else if(action==='add-col'&&cols<8)rows.forEach(row=>row.splice(index+1,0,''));
    else if(action==='remove-col'&&cols>1)rows.forEach(row=>row.splice(index,1));
    else return false;
    render();
    return true;
  }

  function renderCategories(){
    const root=document.getElementById('latex-guide-categories');
    if(!root)return;
    root.innerHTML=['',...categories].map(category=>`<button type="button" class="latex-category${category===state.category?' on':''}" data-category="${escapeHTML(category)}" aria-pressed="${category===state.category}">${escapeHTML(category||'全部')}</button>`).join('');
  }

  function selectTarget(targetId){
    if(!['s-problem','s-note'].includes(targetId))return;
    state.targetId=targetId;updateTargetLabel();
  }

  function entryById(id){return catalog.find(entry=>entry.id===id)}

  async function copyEntry(entry,button){
    try{
      await window.Cuoti.ui.editor.copyText(source(entry));
      showToast('LaTeX 代码已复制','success');
      if(button){button.innerHTML=icon('check')+'已复制';window.setTimeout(()=>{if(button.isConnected)button.innerHTML=icon('clipboard')+'复制'},1200)}
    }catch(error){showToast('复制失败：'+error.message,'error')}
  }

  function insertEntry(entry){
    const target=document.getElementById(state.targetId);
    const payload=entry.matrix?{before:entry.before,after:entry.after,placeholder:serializeMatrix()}:entry.choices?{before:'',after:'',placeholder:source(entry)}:entry;
    window.Cuoti.ui.editor.insertAtSelection(target,{...payload,useSelection:false,selectContent:false});
    showToast(`已插入到${state.targetId==='s-note'?'备注':'题目'}`,'success');
  }

  function insertPinned(id){
    const entry=pinned[id];if(entry)insertEntry(entry);
  }

  function bind(){
    const root=document.querySelector?.('.latex-guide')||document.getElementById('latex-cheatsheet');
    if(!root)return;
    renderCategories();render();updateTargetLabel();
    document.getElementById('latex-guide-search')?.addEventListener('input',render);
    document.getElementById('latex-guide-categories')?.addEventListener('click',event=>{
      const button=event.target.closest('[data-category]');if(!button)return;
      state.category=button.dataset.category||'';renderCategories();render();
    });
    document.getElementById('latex-guide-targets')?.addEventListener('click',event=>{
      const button=event.target.closest('[data-target]');if(button)selectTarget(button.dataset.target);
    });
    root.addEventListener('click',event=>{
      const pinnedButton=event.target.closest('.js-latex-pinned-insert');
      if(pinnedButton){insertPinned(pinnedButton.dataset.pinnedId);return;}
      const matrixButton=event.target.closest('.latex-matrix-control');
      if(matrixButton&&!matrixButton.disabled){matrixAction(matrixButton.dataset.matrixAction,Number(matrixButton.dataset.matrixIndex));return;}
      const choiceButton=event.target.closest('[data-choice-entry]');
      if(choiceButton){selectChoice(choiceButton.dataset.choiceEntry,choiceButton.dataset.choiceValue,choiceButton.closest('.latex-snippet'));return;}
      const button=event.target.closest('.js-latex-copy,.js-latex-insert');if(!button)return;
      const entry=entryById(button.dataset.snippetId);if(!entry)return;
      if(button.classList.contains('js-latex-copy'))void copyEntry(entry,button);else insertEntry(entry);
    });
    root.addEventListener('input',event=>{
      const cell=event.target.closest('.latex-matrix-cell');if(!cell)return;
      matrixState.rows[Number(cell.dataset.matrixRow)][Number(cell.dataset.matrixCol)]=cell.value;
      updateMatrixPreview();
    });
    for(const id of ['s-problem','s-note']){
      document.getElementById(id)?.addEventListener('focus',()=>selectTarget(id));
    }
  }

  window.Cuoti.latexGuide=Object.freeze({catalog,categories,source,filtered,render,selectTarget,serializeMatrix,matrixState,pinnedSource,choiceState,choiceGlyphs,selectChoice,matrixAction,insertEntry});
  bind();
})();
