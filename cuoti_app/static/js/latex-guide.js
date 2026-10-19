(function initLatexGuide(){
  'use strict';

  /* 错题录入的 LaTeX 辅助：输入框上方的公式工具条、符号浮层与可停靠的速查面板。
     插入时在公式外自动补定界符（默认 $…$ / $$…$$，可切换为 \(…\) / \[…\]），
     已在公式里则只插入命令；占位内容插入后被选中，直接输入即可替换。 */

  const DELIMITERS=Object.freeze({
    dollar:Object.freeze({inline:['$','$'],display:['$$','$$']}),
    paren:Object.freeze({inline:['\\(','\\)'],display:['\\[','\\]']}),
  });

  // 工具条：delim 为定界符按钮，ins 为插入规则（ph 为插入后选中的占位），pop 打开符号浮层
  const toolbar=Object.freeze([
    [{id:'inline',delim:'inline',tip:'行内公式'},{id:'display',delim:'display',tip:'独立公式'}],
    [{id:'frac',tip:'分数  \\frac',html:'<span class="latex-tb-frac"><span>a</span><span>b</span></span>',ins:{before:'\\frac{',after:'}{}',ph:'a'}},
     {id:'sqrt',tip:'根式  \\sqrt',html:'<span class="latex-tb-glyph">√x</span>',ins:{before:'\\sqrt{',after:'}',ph:'x'}},
     {id:'sup',tip:'上标  ^{ }',html:'<span class="latex-tb-glyph">x²</span>',ins:{before:'^{',after:'}',ph:'2'}},
     {id:'sub',tip:'下标  _{ }',html:'<span class="latex-tb-glyph">xₙ</span>',ins:{before:'_{',after:'}',ph:'n'}}],
    [{id:'sum',tip:'求和  \\sum',html:'<span class="latex-tb-glyph">∑</span>',ins:{before:'\\sum_{n=1}^{\\infty} ',after:'',ph:'u_n'}},
     {id:'int',tip:'积分  \\int',html:'<span class="latex-tb-glyph">∫</span>',ins:{before:'\\int_{a}^{b} ',after:'\\,dx',ph:'f(x)'}},
     {id:'lim',tip:'极限  \\lim',html:'<span class="latex-tb-glyph latex-tb-word">lim</span>',ins:{before:'\\lim_{n\\to\\infty} ',after:'',ph:'a_n'}}],
    [{id:'greek',tip:'希腊字母',glyph:'α',pop:'greek'},
     {id:'rel',tip:'大小与关系',glyph:'≤',pop:'rel'},
     {id:'ops',tip:'运算与集合',glyph:'±',pop:'ops'},
     {id:'arrows',tip:'箭头与逻辑',glyph:'⇒',pop:'arrows'}],
  ].map(group=>Object.freeze(group.map(Object.freeze))));

  const glyphs=pairs=>Object.freeze(pairs.map(([glyph,cmd,keywords=''])=>Object.freeze({glyph,cmd,keywords})));
  const symbols=Object.freeze({
    greek:{title:'希腊字母',sections:[
      {name:'小写',glyphs:glyphs([['α','\\alpha','阿尔法'],['β','\\beta','贝塔'],['γ','\\gamma','伽马'],['δ','\\delta','德尔塔'],['ε','\\varepsilon','伊普西龙'],['ϵ','\\epsilon','伊普西龙'],['ζ','\\zeta'],['η','\\eta'],['θ','\\theta','西塔'],['ϑ','\\vartheta','西塔'],['κ','\\kappa'],['λ','\\lambda','拉姆达'],['μ','\\mu','缪'],['ν','\\nu'],['ξ','\\xi'],['π','\\pi','派'],['ρ','\\rho','肉'],['σ','\\sigma','西格玛'],['τ','\\tau'],['υ','\\upsilon'],['φ','\\varphi','斐'],['ϕ','\\phi','斐'],['χ','\\chi'],['ψ','\\psi'],['ω','\\omega','欧米伽']])},
      {name:'大写',glyphs:glyphs([['Γ','\\Gamma'],['Δ','\\Delta','德尔塔'],['Θ','\\Theta'],['Λ','\\Lambda'],['Ξ','\\Xi'],['Π','\\Pi'],['Σ','\\Sigma','西格玛'],['Φ','\\Phi'],['Ψ','\\Psi'],['Ω','\\Omega','欧米伽']])},
    ]},
    rel:{title:'大小与关系',sections:[
      {name:'大小',glyphs:glyphs([['<','<','小于'],['>','>','大于'],['≤','\\le','小于等于'],['≥','\\ge','大于等于'],['⩽','\\leqslant','小于等于 斜'],['⩾','\\geqslant','大于等于 斜'],['≪','\\ll','远小于'],['≫','\\gg','远大于']])},
      {name:'相等与近似',glyphs:glyphs([['=','=','等于'],['≠','\\neq','不等于'],['≈','\\approx','约等于'],['≡','\\equiv','恒等 同余'],['∼','\\sim','等价 相似'],['≃','\\simeq','渐近相等'],['≅','\\cong','全等 同构'],['∝','\\propto','正比']])},
      {name:'常用片段',chips:Object.freeze([
        {label:'0<|x−x₀|<δ',cmd:'0<|x-x_0|<\\delta'},
        {label:'|aₙ−A|<ε',cmd:'|a_n-A|<\\varepsilon'},
        {label:'aₙ≤bₙ≤cₙ',cmd:'a_n\\le b_n\\le c_n'},
        {label:'|a+b|≤|a|+|b|',cmd:'|a+b|\\le|a|+|b|'},
        {label:'0<l<+∞',cmd:'0<l<+\\infty'},
        {label:'ρ<1',cmd:'\\rho<1'},
      ].map(Object.freeze))},
    ]},
    ops:{title:'运算与集合',sections:[
      {name:'运算',glyphs:glyphs([['×','\\times','乘'],['·','\\cdot','点乘'],['±','\\pm','正负'],['∓','\\mp','负正'],['÷','\\div','除'],['∞','\\infty','无穷'],['∂','\\partial','偏导'],['∇','\\nabla','梯度']])},
      {name:'集合',glyphs:glyphs([['∈','\\in','属于'],['∉','\\notin','不属于'],['⊂','\\subset','真子集'],['⊆','\\subseteq','子集'],['⊇','\\supseteq','包含'],['∅','\\varnothing','空集'],['∪','\\cup','并集'],['∩','\\cap','交集']])},
      {name:'数集',glyphs:glyphs([['ℝ','\\mathbb{R}','实数'],['ℕ','\\mathbb{N}','自然数'],['ℕ⁺','\\mathbb{N}^+','正整数'],['ℤ','\\mathbb{Z}','整数'],['ℚ','\\mathbb{Q}','有理数'],['ℂ','\\mathbb{C}','复数']])},
    ]},
    arrows:{title:'箭头与逻辑',sections:[
      {name:'箭头',glyphs:glyphs([['→','\\to','趋于 映射'],['↦','\\mapsto','映射'],['⟶','\\longrightarrow'],['↔','\\leftrightarrow'],['↑','\\uparrow','单调递增'],['↓','\\downarrow','单调递减'],['↗','\\nearrow'],['↘','\\searrow']])},
      {name:'逻辑',glyphs:glyphs([['⇒','\\Rightarrow','推出'],['⇐','\\Leftarrow'],['⇔','\\Leftrightarrow','等价 当且仅当'],['∀','\\forall','任意'],['∃','\\exists','存在'],['∴','\\therefore','所以'],['∵','\\because','因为'],['¬','\\neg','非']])},
    ]},
  });

  // 速查条目：block 为长公式，公式外插入时用独立公式包裹；没有 ins 的条目整段插入
  const catalog=Object.freeze([
    {id:'inline',cat:'定界',name:'行内公式',src:'x^2+y^2=r^2',wrap:'inline',keywords:'dollar 美元'},
    {id:'display',cat:'定界',name:'独立公式',src:'E=mc^2',wrap:'display',keywords:'居中 换行 双美元'},
    {id:'script',cat:'上下标',name:'上下标',src:'a_{n+1}=x^{n+1}',ins:{before:'a_{',after:'}',ph:'n+1'},keywords:'指数 下标'},
    {id:'frac',cat:'上下标',name:'分数',src:'\\frac{a}{b}',ins:{before:'\\frac{',after:'}{b}',ph:'a'}},
    {id:'root',cat:'上下标',name:'根式',src:'\\sqrt[n]{x}',ins:{before:'\\sqrt[n]{',after:'}',ph:'x'},keywords:'根号 开方'},

    {id:'lim',cat:'微积分',group:'基础',name:'极限',src:'\\lim_{x\\to0}\\frac{\\sin x}{x}=1',ins:{before:'\\lim_{x\\to ',after:'}',ph:'0'},keywords:'趋于'},
    {id:'famous-limits',cat:'微积分',group:'基础',name:'两个重要极限',src:'\\lim_{x\\to0}\\frac{\\sin x}{x}=1,\\quad\\lim_{x\\to\\infty}\\left(1+\\frac{1}{x}\\right)^{x}=e',block:true},
    {id:'sum-prod',cat:'微积分',group:'基础',name:'求和与连乘',src:'\\sum_{k=1}^{n}a_k,\\quad\\prod_{k=1}^{n}a_k',ins:{before:'\\sum_{k=1}^{n} ',after:'',ph:'a_k'},keywords:'sigma 乘积'},
    {id:'int',cat:'微积分',group:'基础',name:'定积分',src:'\\int_a^b f(x)\\,dx',ins:{before:'\\int_{a}^{b} ',after:'\\,dx',ph:'f(x)'}},
    {id:'deriv',cat:'微积分',group:'基础',name:'导数',src:'\\frac{dy}{dx},\\ f\'(x_0)',ins:{before:'\\frac{d',after:'}{dx}',ph:'y'},keywords:'求导 微分'},
    {id:'eps-n',cat:'微积分',group:'极限定义',name:'数列极限 ε-N',src:'\\lim_{n\\to\\infty}a_n=A\\iff\\forall\\varepsilon>0,\\ \\exists N\\in\\mathbb{N}^+,\\ \\forall n>N:\\ |a_n-A|<\\varepsilon',block:true},
    {id:'eps-delta',cat:'微积分',group:'极限定义',name:'函数极限 ε-δ',src:'\\lim_{x\\to x_0}f(x)=A\\iff\\forall\\varepsilon>0,\\ \\exists\\delta>0,\\ \\forall x:\\ 0<|x-x_0|<\\delta\\Rightarrow|f(x)-A|<\\varepsilon',block:true},
    {id:'uniform-continuity',cat:'微积分',group:'极限定义',name:'一致连续',src:'\\forall\\varepsilon>0,\\ \\exists\\delta>0,\\ \\forall x_1,x_2\\in I:\\ |x_1-x_2|<\\delta\\Rightarrow|f(x_1)-f(x_2)|<\\varepsilon',block:true},
    {id:'uniform-convergence',cat:'微积分',group:'极限定义',name:'函数列一致收敛',src:'\\forall\\varepsilon>0,\\ \\exists N,\\ \\forall n>N,\\ \\forall x\\in I:\\ |f_n(x)-f(x)|<\\varepsilon',block:true},

    {id:'cauchy-sequence',cat:'级数',group:'收敛准则',name:'柯西收敛准则（数列）',src:'\\{a_n\\}\\text{ 收敛}\\iff\\forall\\varepsilon>0,\\ \\exists N\\in\\mathbb{N}^+,\\ \\forall m,n>N:\\ |a_m-a_n|<\\varepsilon',block:true},
    {id:'cauchy-series',cat:'级数',group:'收敛准则',name:'柯西收敛准则（级数）',src:'\\sum_{n=1}^{\\infty}u_n\\text{ 收敛}\\iff\\forall\\varepsilon>0,\\ \\exists N\\in\\mathbb{N}^+,\\ \\forall n>N,\\ \\forall p\\in\\mathbb{N}^+:\\ \\left|\\sum_{k=n+1}^{n+p}u_k\\right|<\\varepsilon',block:true},
    {id:'cauchy-product',cat:'级数',group:'收敛准则',name:'柯西乘积',src:'\\left(\\sum_{n=0}^{\\infty}a_n\\right)\\left(\\sum_{n=0}^{\\infty}b_n\\right)=\\sum_{n=0}^{\\infty}c_n,\\quad c_n=\\sum_{k=0}^{n}a_kb_{n-k}',block:true},
    {id:'necessary-condition',cat:'级数',group:'收敛准则',name:'收敛的必要条件',src:'\\sum_{n=1}^{\\infty}u_n\\text{ 收敛}\\Rightarrow\\lim_{n\\to\\infty}u_n=0',block:true},
    {id:'comparison-test',cat:'级数',group:'正项级数判别',name:'比较判别法（极限形式）',src:'\\lim_{n\\to\\infty}\\frac{u_n}{v_n}=l\\ (0<l<+\\infty)\\Rightarrow\\sum u_n\\text{ 与 }\\sum v_n\\text{ 同敛散}',block:true},
    {id:'ratio-test',cat:'级数',group:'正项级数判别',name:'比值判别法（达朗贝尔）',src:'\\lim_{n\\to\\infty}\\frac{u_{n+1}}{u_n}=\\rho\\ \\begin{cases}\\rho<1, & \\text{收敛}\\\\ \\rho>1, & \\text{发散}\\end{cases}',block:true},
    {id:'root-test',cat:'级数',group:'正项级数判别',name:'根值判别法（柯西）',src:'\\lim_{n\\to\\infty}\\sqrt[n]{u_n}=\\rho\\ \\begin{cases}\\rho<1, & \\text{收敛}\\\\ \\rho>1, & \\text{发散}\\end{cases}',block:true},
    {id:'integral-test',cat:'级数',group:'正项级数判别',name:'积分判别法',src:'\\sum_{n=1}^{\\infty}f(n)\\text{ 与 }\\int_{1}^{+\\infty}f(x)\\,dx\\text{ 同敛散}',block:true},
    {id:'p-series',cat:'级数',group:'正项级数判别',name:'p 级数',src:'\\sum_{n=1}^{\\infty}\\frac{1}{n^p}\\ \\begin{cases}p>1, & \\text{收敛}\\\\ p\\le1, & \\text{发散}\\end{cases}',block:true},
    {id:'leibniz-test',cat:'级数',group:'一般项级数',name:'莱布尼茨判别法',src:'u_n\\ge u_{n+1}>0,\\ \\lim_{n\\to\\infty}u_n=0\\Rightarrow\\sum_{n=1}^{\\infty}(-1)^{n-1}u_n\\text{ 收敛}',block:true,keywords:'交错级数'},
    {id:'dirichlet-test',cat:'级数',group:'一般项级数',name:'狄利克雷判别法',src:'\\left|\\sum_{k=1}^{n}a_k\\right|\\le M,\\ b_n\\text{ 单调趋于 }0\\Rightarrow\\sum_{n=1}^{\\infty}a_nb_n\\text{ 收敛}',block:true},
    {id:'abel-test',cat:'级数',group:'一般项级数',name:'阿贝尔判别法',src:'\\sum_{n=1}^{\\infty}a_n\\text{ 收敛},\\ \\{b_n\\}\\text{ 单调有界}\\Rightarrow\\sum_{n=1}^{\\infty}a_nb_n\\text{ 收敛}',block:true},
    {id:'abel-summation',cat:'级数',group:'一般项级数',name:'阿贝尔变换',src:'\\sum_{k=1}^{n}a_kb_k=A_nb_n-\\sum_{k=1}^{n-1}A_k\\left(b_{k+1}-b_k\\right),\\quad A_k=\\sum_{i=1}^{k}a_i',block:true,keywords:'分部求和'},
    {id:'weierstrass-test',cat:'级数',group:'函数项与幂级数',name:'魏尔斯特拉斯 M 判别法',src:'|u_n(x)|\\le M_n,\\ \\sum_{n=1}^{\\infty}M_n<+\\infty\\Rightarrow\\sum_{n=1}^{\\infty}u_n(x)\\text{ 一致收敛}',block:true,keywords:'优级数'},
    {id:'radius-ratio',cat:'级数',group:'函数项与幂级数',name:'收敛半径（比值）',src:'R=\\lim_{n\\to\\infty}\\left|\\frac{a_n}{a_{n+1}}\\right|',block:true},
    {id:'radius-root',cat:'级数',group:'函数项与幂级数',name:'收敛半径（柯西-阿达马）',src:'R=\\frac{1}{\\limsup\\limits_{n\\to\\infty}\\sqrt[n]{|a_n|}}',block:true},
    {id:'taylor-series',cat:'级数',group:'函数项与幂级数',name:'泰勒级数',src:'f(x)=\\sum_{n=0}^{\\infty}\\frac{f^{(n)}(x_0)}{n!}(x-x_0)^n',block:true},
    {id:'expand-exp',cat:'级数',group:'常用展开',name:'eˣ',src:'e^x=\\sum_{n=0}^{\\infty}\\frac{x^n}{n!},\\quad x\\in(-\\infty,+\\infty)',block:true,keywords:'麦克劳林 指数'},
    {id:'expand-geometric',cat:'级数',group:'常用展开',name:'1/(1−x)',src:'\\frac{1}{1-x}=\\sum_{n=0}^{\\infty}x^n,\\quad |x|<1',block:true,keywords:'麦克劳林 几何级数'},
    {id:'expand-log',cat:'级数',group:'常用展开',name:'ln(1+x)',src:'\\ln(1+x)=\\sum_{n=1}^{\\infty}\\frac{(-1)^{n-1}}{n}x^n,\\quad x\\in(-1,1]',block:true,keywords:'麦克劳林 对数'},
    {id:'expand-sin',cat:'级数',group:'常用展开',name:'sin x',src:'\\sin x=\\sum_{n=0}^{\\infty}\\frac{(-1)^n}{(2n+1)!}x^{2n+1}',block:true,keywords:'麦克劳林 正弦'},
    {id:'expand-cos',cat:'级数',group:'常用展开',name:'cos x',src:'\\cos x=\\sum_{n=0}^{\\infty}\\frac{(-1)^n}{(2n)!}x^{2n}',block:true,keywords:'麦克劳林 余弦'},
    {id:'expand-binomial',cat:'级数',group:'常用展开',name:'(1+x)^α',src:'(1+x)^{\\alpha}=\\sum_{n=0}^{\\infty}\\binom{\\alpha}{n}x^n,\\quad |x|<1',block:true,keywords:'麦克劳林 二项'},
    {id:'fourier-series',cat:'级数',group:'傅里叶级数',name:'傅里叶级数',src:'f(x)\\sim\\frac{a_0}{2}+\\sum_{n=1}^{\\infty}\\left(a_n\\cos nx+b_n\\sin nx\\right)',block:true},
    {id:'fourier-coefficients',cat:'级数',group:'傅里叶级数',name:'傅里叶系数',src:'a_n=\\frac{1}{\\pi}\\int_{-\\pi}^{\\pi}f(x)\\cos nx\\,dx,\\quad b_n=\\frac{1}{\\pi}\\int_{-\\pi}^{\\pi}f(x)\\sin nx\\,dx',block:true},

    {id:'triangle-inequality',cat:'关系',group:'常用不等式',name:'三角不等式',src:'|a+b|\\le|a|+|b|,\\quad\\big||a|-|b|\\big|\\le|a-b|',block:true,keywords:'绝对值'},
    {id:'mean-inequality',cat:'关系',group:'常用不等式',name:'均值不等式',src:'\\frac{2}{\\frac{1}{a}+\\frac{1}{b}}\\le\\sqrt{ab}\\le\\frac{a+b}{2}\\le\\sqrt{\\frac{a^2+b^2}{2}}\\quad(a,b>0)',block:true,keywords:'调和 几何 算术 平方'},
    {id:'cauchy-schwarz',cat:'关系',group:'常用不等式',name:'柯西-施瓦茨不等式',src:'\\left(\\sum_{k=1}^{n}a_kb_k\\right)^2\\le\\left(\\sum_{k=1}^{n}a_k^2\\right)\\left(\\sum_{k=1}^{n}b_k^2\\right)',block:true},
    {id:'cauchy-schwarz-integral',cat:'关系',group:'常用不等式',name:'柯西-施瓦茨（积分）',src:'\\left(\\int_a^b f(x)g(x)\\,dx\\right)^2\\le\\int_a^b f^2(x)\\,dx\\int_a^b g^2(x)\\,dx',block:true},
    {id:'bernoulli-inequality',cat:'关系',group:'常用不等式',name:'伯努利不等式',src:'(1+x)^n\\ge1+nx\\quad(x>-1,\\ n\\in\\mathbb{N}^+)',block:true},
    {id:'squeeze',cat:'关系',group:'常用不等式',name:'夹逼准则',src:'a_n\\le b_n\\le c_n,\\ \\lim_{n\\to\\infty}a_n=\\lim_{n\\to\\infty}c_n=A\\Rightarrow\\lim_{n\\to\\infty}b_n=A',block:true,keywords:'迫敛'},
    {id:'sin-x-tan',cat:'关系',group:'函数不等式',name:'sin x < x < tan x',src:'\\sin x<x<\\tan x\\quad\\left(0<x<\\frac{\\pi}{2}\\right)',block:true},
    {id:'log-inequality',cat:'关系',group:'函数不等式',name:'对数不等式',src:'\\frac{x}{1+x}<\\ln(1+x)<x\\quad(x>0)',block:true},
    {id:'exp-inequality',cat:'关系',group:'函数不等式',name:'指数不等式',src:'e^x\\ge1+x\\quad(x\\in\\mathbb{R})',block:true},

    {id:'functions',cat:'函数',name:'常用函数',src:'\\sin x+\\cos x+\\ln x+e^{x}+\\arctan x',ins:{before:'\\sin ',after:'',ph:'x'},keywords:'正弦 余弦 对数 指数 反三角'},
    {id:'sets',cat:'集合逻辑',name:'集合',src:'x\\in\\mathbb{R},\\ A\\cup B,\\ A\\cap B,\\ A\\subseteq B',ins:{before:'',after:'\\in\\mathbb{R}',ph:'x'},keywords:'属于 实数 并集 交集'},
    {id:'logic',cat:'集合逻辑',name:'推出 / 等价',src:'P\\Rightarrow Q,\\ P\\Leftrightarrow Q',ins:{before:'',after:'\\Rightarrow Q',ph:'P'},keywords:'当且仅当'},
    {id:'quantifiers',cat:'集合逻辑',name:'量词',src:'\\forall x\\in D,\\ \\exists y\\in D',ins:{before:'\\forall ',after:'\\in D',ph:'x'},keywords:'任意 存在'},
    {id:'brackets',cat:'括号',name:'自动伸缩括号',src:'\\left(\\frac{a}{b}\\right),\\ \\left\\{x\\mid x>0\\right\\}',ins:{before:'\\left(',after:'\\right)',ph:'x'},keywords:'圆括号 大括号'},
    {id:'norm-vector',cat:'括号',name:'绝对值 / 范数 / 向量',src:'|x|,\\ \\lVert v\\rVert,\\ \\vec{a},\\ \\boldsymbol{x}',ins:{before:'\\left|',after:'\\right|',ph:'x'},keywords:'模 长度 粗体'},
    {id:'cases',cat:'多行',name:'分段函数',src:'f(x)=\\begin{cases}x^2, & x\\ge0\\\\ -x, & x<0\\end{cases}',block:true,ins:{before:'f(x)=\\begin{cases}',after:'\\end{cases}',ph:'x^2, & x\\ge0\\\\ -x, & x<0'}},
    {id:'system',cat:'多行',name:'方程组',src:'\\begin{cases}x+y=1\\\\ x-y=0\\end{cases}',block:true,ins:{before:'\\begin{cases}',after:'\\end{cases}',ph:'x+y=1\\\\ x-y=0'}},
    {id:'aligned',cat:'多行',name:'多行对齐',src:'\\begin{aligned}a&=b+c\\\\ &=d+e\\end{aligned}',block:true,ins:{before:'\\begin{aligned}',after:'\\end{aligned}',ph:'a&=b+c\\\\ &=d+e'},keywords:'等号对齐'},
    {id:'text-space',cat:'文字',name:'公式内文字与空格',src:'x>0\\quad\\text{时函数递增}',ins:{before:'\\text{',after:'}',ph:'文字'},keywords:'text quad 中文'},
  ].map(item=>Object.freeze({...item,ins:item.ins&&Object.freeze(item.ins)})));

  const categories=Object.freeze(['最近使用','全部','定界','上下标','微积分','级数','关系','符号','函数','集合逻辑','括号','矩阵','多行','文字']);
  const MATRIX_ENVIRONMENTS=Object.freeze(['pmatrix','bmatrix','vmatrix']);

  const byId=new Map(catalog.map(item=>[item.id,item]));
  const toolById=new Map(toolbar.flat().map(button=>[button.id,button]));

  // 光标前是否已在公式里：识别 $ / $$ / \( \) / \[ \]，跳过 \$ 等转义字符
  function mathContextAt(text,position){
    const source=String(text||'');
    const end=Math.max(0,Math.min(source.length,Number(position)||0));
    let inline=false,display=false,paren=0,bracket=0;
    for(let index=0;index<end;index++){
      const char=source[index];
      if(char==='\\'){
        const next=source[index+1];
        if(next==='(')paren++;
        else if(next===')')paren=Math.max(0,paren-1);
        else if(next==='[')bracket++;
        else if(next===']')bracket=Math.max(0,bracket-1);
        index++;
        continue;
      }
      if(char!=='$')continue;
      if(source[index+1]==='$'&&!inline){display=!display;index++;}
      else if(!display)inline=!inline;
    }
    return inline||display||paren>0||bracket>0;
  }

  function delimitersFor(style,kind){
    return (DELIMITERS[style]||DELIMITERS.dollar)[kind==='display'?'display':'inline'];
  }

  // 把一次插入换算成 editor.insertAtSelection 的参数。wrap 强制定界符；否则公式外按 block 补行内或独立定界符。
  // 有占位（ph）或要求光标停在开头（caret:'start'）时保留选区内容并选中；整段公式则替换选区、光标落在末尾。
  function planInsertion(text,selectionStart,spec={},style='dollar'){
    const {before='',after='',ph='',wrap=null,block=false,caret=null}=spec||{};
    const kind=wrap||(mathContextAt(text,selectionStart)?null:(block?'display':'inline'));
    const [open,close]=kind?delimitersFor(style,kind):['',''];
    const keepContent=Boolean(ph)||caret==='start';
    return {before:open+before,after:after+close,placeholder:ph,useSelection:keepContent,selectContent:keepContent};
  }

  // 公式内的字母命令后补空格，避免与后续字母粘连（\alpha x）；公式外会被包裹，无需空格
  function commandInsertion(text,selectionStart,command){
    const value=String(command||'');
    const spaced=mathContextAt(text,selectionStart)&&/^\\[A-Za-z]+$/.test(value);
    return {before:spaced?value+' ':value};
  }

  // 空矩阵：光标落在第一个元素，其余位置留空逐格填写
  function matrixInsertion(rows,cols,environment='pmatrix'){
    const clamp=value=>Math.max(1,Math.min(6,Math.trunc(Number(value))||1));
    const env=MATRIX_ENVIRONMENTS.includes(environment)?environment:'pmatrix';
    const body=Array.from({length:clamp(rows)},()=>Array(clamp(cols)).fill('').join(' & ')).join(' \\\\\n');
    return {before:`\\begin{${env}}\n`,after:`${body}\n\\end{${env}}`,ph:'',block:true,caret:'start'};
  }

  function normalizedQuery(query){return String(query||'').trim().toLowerCase();}

  function filterCatalog({query='',category='全部',recent=[]}={}){
    const needle=normalizedQuery(query);
    if(needle)return catalog.filter(item=>`${item.cat} ${item.group||''} ${item.name} ${item.keywords||''} ${item.src}`.toLowerCase().includes(needle));
    if(category==='最近使用')return recent.map(id=>byId.get(id)).filter(Boolean);
    if(category==='全部')return catalog.slice();
    return catalog.filter(item=>item.cat===category);
  }

  // 按符号、命令、中文名或分组名检索符号；返回 [{key,title,name,glyphs}]
  function filterSymbols(query){
    const raw=String(query||'').trim();
    const needle=raw.toLowerCase();
    if(!needle)return [];
    const results=[];
    for(const [key,group] of Object.entries(symbols)){
      for(const section of group.sections){
        if(!section.glyphs)continue;
        const sectionHit=`${group.title}${section.name}`.includes(raw);
        const found=section.glyphs.filter(entry=>sectionHit||entry.glyph===raw||entry.cmd.toLowerCase().includes(needle)||entry.keywords.includes(raw));
        if(found.length)results.push({key,title:group.title,name:section.name,glyphs:found});
      }
    }
    return results;
  }

  // ── 界面：工具条、符号浮层、停靠速查 ──
  const STORAGE=Object.freeze({wrap:'cuoti.latexGuide.wrap',recent:'cuoti.latexGuide.recent',dock:'cuoti.latexGuide.dock',category:'cuoti.latexGuide.category'});
  const DOCKED_QUERY='(min-width:1101px)';
  const RECENT_LIMIT=8;
  const state={wrap:'dollar',category:'全部',query:'',recent:[],targetId:'s-problem',matrix:{rows:2,cols:2,env:'pmatrix'}};
  let popover=null,popoverOpener=null,peek=null,peekId=null,noticeTimer=0;
  const peekCache=new Map();

  function readStorage(key,fallback){
    try{const value=window.localStorage?.getItem(key);return value==null?fallback:value;}catch{return fallback;}
  }
  function writeStorage(key,value){
    try{window.localStorage?.setItem(key,value);}catch{/* storage may be unavailable */}
  }
  function isDocked(){return Boolean(window.matchMedia?.(DOCKED_QUERY).matches);}
  function dock(){return document.getElementById('latex-dock');}
  function targetArea(){return document.getElementById(state.targetId);}

  function delimLabel(kind){
    const [open,close]=delimitersFor(state.wrap,kind);
    return `<span class="latex-tb-delim">${escapeHTML(open)}<i>x</i>${escapeHTML(close)}</span>`;
  }

  function toolbarHTML(){
    const groups=toolbar.map(group=>`<div class="tb-group">${group.map(button=>{
      const common=`type="button" class="latex-tb-btn" tabindex="-1" data-tip="${escapeHTML(button.tip)}" aria-label="${escapeHTML(button.tip)}"`;
      if(button.delim)return `<button ${common} data-latex-delim="${button.delim}">${delimLabel(button.delim)}</button>`;
      if(button.pop)return `<button ${common} data-latex-pop="${button.pop}" aria-haspopup="true" aria-expanded="false"><span class="latex-tb-glyph">${escapeHTML(button.glyph)}</span>${icon('chevronDown','latex-tb-caret')}</button>`;
      return `<button ${common} data-latex-tool="${button.id}">${button.html}</button>`;
    }).join('')}</div>`).join('<div class="tb-divider" aria-hidden="true"></div>');
    return groups+`<button type="button" class="latex-tb-btn latex-tb-more" tabindex="-1" data-latex-dock-toggle aria-controls="latex-dock" aria-expanded="false" data-tip="完整速查  Ctrl+/">${icon('bookOpen')}<span>速查</span></button>`;
  }

  function renderToolbars(){
    document.querySelectorAll('[data-latex-toolbar]').forEach(bar=>{
      bar.innerHTML=toolbarHTML();
      bar.querySelector('.latex-tb-btn')?.setAttribute('tabindex','0');
    });
    syncDockToggles();
  }

  function refreshDelimLabels(){
    document.querySelectorAll('[data-latex-delim]').forEach(button=>{button.innerHTML=delimLabel(button.dataset.latexDelim);});
  }

  function setTarget(id){
    if(!['s-problem','s-note'].includes(id))return;
    state.targetId=id;
    document.querySelectorAll('[data-latex-editor]').forEach(wrap=>wrap.classList.toggle('is-active',wrap.dataset.latexEditor===id));
    const label=document.getElementById('latex-dock-target');
    if(label)label.textContent=id==='s-note'?'备注':'题目';
  }

  // ── 插入与轻提示 ──
  function applyInsertion(area,spec,recentId=null){
    if(!area)return;
    const previous={value:area.value,start:area.selectionStart,end:area.selectionEnd};
    const plan=planInsertion(area.value,area.selectionStart,spec,state.wrap);
    window.Cuoti.ui.editor.insertAtSelection(area,{...plan,preventScroll:true});
    if(recentId)remember(recentId);
    showInsertNotice(area,previous,Boolean(spec.ph));
  }

  function insertCommand(area,command){
    if(area)applyInsertion(area,commandInsertion(area.value,area.selectionStart,command));
  }

  function showInsertNotice(area,previous,placeholderSelected){
    const host=area.closest('.editor-body');
    if(!host)return;
    host.querySelector('.latex-insert-notice')?.remove();
    const notice=document.createElement('div');
    notice.className='latex-insert-notice';
    notice.setAttribute('role','status');
    notice.innerHTML=`${icon('check')}<span>${placeholderSelected?'已插入，占位已选中':'已插入'}</span><button type="button">撤销</button>`;
    const undo=notice.querySelector('button');
    undo.addEventListener('mousedown',event=>event.preventDefault());
    undo.addEventListener('click',()=>{
      area.value=previous.value;
      area.focus({preventScroll:true});
      area.setSelectionRange(previous.start,previous.end);
      area.dispatchEvent(new Event('input',{bubbles:true}));
      notice.remove();
    });
    host.appendChild(notice);
    window.clearTimeout(noticeTimer);
    noticeTimer=window.setTimeout(()=>notice.remove(),2600);
  }

  function remember(id){
    state.recent=[id,...state.recent.filter(item=>item!==id)].slice(0,RECENT_LIMIT);
    writeStorage(STORAGE.recent,JSON.stringify(state.recent));
  }

  // ── 符号浮层：固定定位，避免被可横向滚动的工具条裁切 ──
  function glyphButtons(list){
    return list.map(entry=>`<button type="button" class="latex-glyph" data-latex-cmd="${escapeHTML(entry.cmd)}" title="${escapeHTML(entry.cmd)}" aria-label="${escapeHTML(entry.cmd)}">${escapeHTML(entry.glyph)}</button>`).join('');
  }

  function popoverHTML(key){
    const group=symbols[key];
    const sections=group.sections.map(section=>`<div class="latex-pop-section">${escapeHTML(section.name)}</div>${section.chips
      ?`<div class="latex-pop-chips">${section.chips.map(chip=>`<button type="button" class="latex-pop-chip" data-latex-cmd="${escapeHTML(chip.cmd)}" title="${escapeHTML(chip.cmd)}">${escapeHTML(chip.label)}</button>`).join('')}</div>`
      :`<div class="latex-glyph-grid">${glyphButtons(section.glyphs)}</div>`}`).join('');
    return `<div class="latex-pop-head"><span>${escapeHTML(group.title)}</span><code data-latex-pop-code>悬停查看命令</code></div>${sections}`;
  }

  function closePopover({restoreFocus=false}={}){
    if(!popover)return;
    const opener=popoverOpener;
    popover.remove();popover=null;popoverOpener=null;
    opener?.setAttribute('aria-expanded','false');
    if(restoreFocus)opener?.focus({preventScroll:true});
  }

  function positionPopover(){
    if(!popover||!popoverOpener)return;
    const anchor=popoverOpener.getBoundingClientRect();
    const width=popover.offsetWidth,height=popover.offsetHeight;
    const left=Math.max(8,Math.min(anchor.left-8,window.innerWidth-width-8));
    const below=anchor.bottom+6;
    popover.style.left=`${left}px`;
    popover.style.top=`${below+height>window.innerHeight-8?Math.max(8,anchor.top-height-6):below}px`;
  }

  function openPopover(opener,fromKeyboard){
    if(popoverOpener===opener){closePopover();return;}
    closePopover();
    const key=opener.dataset.latexPop;
    popover=document.createElement('div');
    popover.className='latex-pop no-math';
    popover.setAttribute('role','dialog');
    popover.setAttribute('aria-label',symbols[key].title);
    popover.dataset.latexTarget=opener.closest('[data-latex-toolbar]').dataset.latexToolbar;
    popover.innerHTML=popoverHTML(key);
    document.body.appendChild(popover);
    popoverOpener=opener;
    opener.setAttribute('aria-expanded','true');
    positionPopover();
    if(fromKeyboard)popover.querySelector('button')?.focus({preventScroll:true});
  }

  // ── 停靠速查 ──
  function dockOpen(){return Boolean(dock()&&!dock().hidden);}

  function syncDockToggles(){
    const open=dockOpen();
    document.querySelectorAll('[data-latex-dock-toggle]').forEach(button=>{
      button.setAttribute('aria-expanded',String(open));
      button.classList.toggle('on',open);
    });
  }

  function setDockOpen(open,{focusSearch=false,persist=true}={}){
    const panel=dock();
    if(!panel)return;
    const docked=isDocked();
    panel.hidden=!open;
    panel.classList.toggle('is-sheet',!docked);
    document.getElementById('p-add')?.classList.toggle('latex-dock-open',open&&docked);
    document.querySelectorAll('.latex-dock-scrim').forEach(scrim=>{scrim.hidden=!(open&&!docked);});
    if(persist&&docked)writeStorage(STORAGE.dock,open?'1':'0');
    syncDockToggles();
    hidePeek();
    if(open){
      panel.classList.remove('is-opening');void panel.offsetWidth;panel.classList.add('is-opening');
      renderDock();
      if(focusSearch)document.getElementById('latex-dock-search')?.focus({preventScroll:true});
    }
  }

  // 视口跨过停靠断点时切换停靠/抽屉形态：宽屏恢复上次开合，窄屏抽屉默认收起
  let lastDocked=null;
  function syncDockMode(){
    const docked=isDocked();
    if(docked===lastDocked)return;
    lastDocked=docked;
    setDockOpen(docked&&readStorage(STORAGE.dock,'1')==='1',{persist:false});
  }

  function renderCategories(){
    const root=document.getElementById('latex-dock-cats');
    if(!root)return;
    const active=state.query.trim()?'':state.category;
    root.innerHTML=categories.map(category=>`<button type="button" class="latex-dock-cat${category===active?' on':''}" data-latex-category="${escapeHTML(category)}" aria-pressed="${category===active}">${escapeHTML(category)}</button>`).join('');
  }

  function wrappedSource(item){
    if(!item.wrap)return item.src;
    const [open,close]=delimitersFor(state.wrap,item.wrap);
    return open+item.src+close;
  }

  function itemHTML(item){
    const id=escapeHTML(item.id),name=escapeHTML(item.name);
    return `<div class="latex-item" data-latex-item="${id}">
      <button type="button" class="latex-item-main" data-latex-insert="${id}" aria-label="插入：${name}">
        <span class="latex-item-math math">${escapeHTML(`$\\displaystyle ${item.src}$`)}</span>
        <span class="latex-item-meta"><span class="latex-item-name">${name}${item.block?'<small>独立</small>':''}</span><code class="no-math">${escapeHTML(wrappedSource(item))}</code></span>
        <span class="latex-item-add" aria-hidden="true">${icon('plus')}</span>
      </button>
      <button type="button" class="latex-item-copy" data-latex-copy="${id}" title="复制 LaTeX" aria-label="复制${name}的 LaTeX">${icon('copy')}</button>
    </div>`;
  }

  function listHTML(items){
    let html='',heading=null;
    for(const item of items){
      const label=state.category==='最近使用'&&!state.query.trim()?'最近使用':item.group?`${item.cat} · ${item.group}`:item.cat;
      if(label!==heading){html+=`<div class="latex-dock-group">${escapeHTML(label)}</div>`;heading=label;}
      html+=itemHTML(item);
    }
    return html;
  }

  function symbolsHTML(groups){
    return groups.map(group=>`<div class="latex-dock-group">${escapeHTML(group.title)} · ${escapeHTML(group.name)}</div><div class="latex-glyph-grid latex-dock-glyphs no-math">${glyphButtons(group.glyphs)}</div>`).join('');
  }

  function sectionsOf(key){
    return symbols[key].sections.filter(section=>section.glyphs).map(section=>({key,title:symbols[key].title,name:section.name,glyphs:section.glyphs}));
  }

  function matrixHTML(){
    const {rows,cols,env}=state.matrix;
    const cells=Array.from({length:36},(_,index)=>{
      const row=Math.floor(index/6)+1,col=index%6+1;
      return `<button type="button" class="latex-size-cell${row<=rows&&col<=cols?' hit':''}" data-latex-size="${row}x${col}" aria-label="${row} 行 ${col} 列"></button>`;
    }).join('');
    const envs=MATRIX_ENVIRONMENTS.map(name=>`<button type="button" data-latex-env="${name}" aria-pressed="${name===env}" class="${name===env?'on':''}">${{pmatrix:'( )',bmatrix:'[ ]',vmatrix:'| |'}[name]}</button>`).join('');
    return `<div class="latex-dock-group">构造器</div><div class="latex-matrix-builder no-math"><div class="latex-matrix-head"><strong>矩阵 / 行列式</strong><span>悬停选尺寸，点击插入</span></div><div class="latex-matrix-row"><div class="latex-size-grid" data-latex-size-grid>${cells}</div><div class="latex-matrix-side"><div class="latex-seg" role="group" aria-label="括号样式">${envs}</div><span class="latex-matrix-size" data-latex-size-label>${rows} × ${cols}</span></div></div></div>`;
  }

  function renderDock(){
    const body=document.getElementById('latex-dock-body');
    if(!body||!dockOpen())return;
    renderCategories();
    const query=state.query.trim();
    let html='';
    if(query){
      html=listHTML(filterCatalog({query}))+symbolsHTML(filterSymbols(query));
      if(!html)html=emptyStateHTML('没有匹配的写法',{icon:'search',description:'换个关键词，或选择上方分类。',compact:true});
    }else if(state.category==='最近使用'){
      html=state.recent.length?listHTML(filterCatalog({category:'最近使用',recent:state.recent})):emptyStateHTML('还没有使用记录',{icon:'search',description:'从速查里插入过的公式会出现在这里。',compact:true});
    }else if(state.category==='符号'){
      html=Object.keys(symbols).flatMap(sectionsOf).map(group=>symbolsHTML([group])).join('');
    }else if(state.category==='矩阵'){
      html=matrixHTML();
    }else if(state.category==='关系'){
      html=symbolsHTML(sectionsOf('rel'))+listHTML(filterCatalog({category:'关系'}));
    }else{
      html=listHTML(filterCatalog({category:state.category}))+(state.category==='全部'?matrixHTML():'');
    }
    body.innerHTML=html;
    body.scrollTop=0;
    hidePeek();
    void renderMath(body).then(()=>fitFormulas(body));
  }

  function setCategory(category){
    if(!categories.includes(category))return;
    state.category=category;state.query='';
    const search=document.getElementById('latex-dock-search');
    if(search)search.value='';
    writeStorage(STORAGE.category,category);
    renderDock();
  }

  // 长公式缩放到面板宽度；缩到 0.7 仍放不下的右缘淡出，悬停时在面板左侧浮出原尺寸全式
  function fitFormulas(root){
    root?.querySelectorAll?.('.latex-item-math').forEach(box=>{
      const formula=box.querySelector('mjx-container');
      if(!formula||!box.clientWidth)return;
      formula.style.zoom='';
      const ratio=(box.clientWidth-2)/formula.getBoundingClientRect().width;
      if(ratio<1)formula.style.zoom=String(Math.max(.7,ratio));
      box.closest('.latex-item').classList.toggle('is-scaled',ratio<.95);
      box.classList.toggle('is-clipped',ratio<.7);
    });
  }

  function hidePeek(){if(peek)peek.hidden=true;peekId=null;}

  function placePeek(item){
    if(!peek||peek.hidden)return;
    const panel=dock().getBoundingClientRect(),row=item.getBoundingClientRect();
    const main=document.querySelector('.main')?.getBoundingClientRect();
    peek.style.right=`${window.innerWidth-panel.left+10}px`;
    peek.style.maxWidth=`${Math.max(260,panel.left-(main?.left||0)-24)}px`;
    peek.style.top=`${Math.max(8,Math.min(row.top,window.innerHeight-peek.offsetHeight-8))}px`;
  }

  function showPeek(item){
    if(!isDocked()||!item?.classList.contains('is-scaled'))return hidePeek();
    const entry=byId.get(item.dataset.latexItem);
    if(!entry||(peekId===entry.id&&!peek.hidden))return;
    if(!peek){
      peek=document.createElement('div');
      peek.className='latex-peek';
      peek.setAttribute('aria-hidden','true');
      document.body.appendChild(peek);
    }
    peekId=entry.id;
    if(!peekCache.has(entry.id)){
      const node=document.createElement('div');
      node.innerHTML=`<div class="latex-peek-title">${escapeHTML(entry.name)}</div><div class="latex-peek-math math">${escapeHTML(`$$${entry.src}$$`)}</div>`;
      peekCache.set(entry.id,node);
    }
    const node=peekCache.get(entry.id);
    peek.replaceChildren(node);
    peek.hidden=false;
    placePeek(item);
    void renderMath(node).then(()=>{if(peekId===entry.id)placePeek(item);});
  }

  function setWrap(style){
    if(!DELIMITERS[style])return;
    state.wrap=style;
    writeStorage(STORAGE.wrap,style);
    document.querySelectorAll('[data-latex-wrap]').forEach(button=>{
      const active=button.dataset.latexWrap===style;
      button.classList.toggle('on',active);button.setAttribute('aria-pressed',String(active));
    });
    refreshDelimLabels();
    renderDock();
  }

  function paintMatrix(rows,cols){
    document.querySelectorAll('[data-latex-size]').forEach(cell=>{
      const [row,col]=cell.dataset.latexSize.split('x').map(Number);
      cell.classList.toggle('hit',row<=rows&&col<=cols);
    });
    const label=document.querySelector('[data-latex-size-label]');
    if(label)label.textContent=`${rows} × ${cols}`;
  }

  async function copySource(item,button){
    try{
      await window.Cuoti.ui.editor.copyText(wrappedSource(item));
      button.innerHTML=icon('check');button.classList.add('is-copied');
      window.setTimeout(()=>{if(button.isConnected){button.innerHTML=icon('copy');button.classList.remove('is-copied');}},1200);
    }catch(error){showToast('复制失败：'+error.message,'error');}
  }

  // 工具条按 WAI-ARIA toolbar 约定只占一个 Tab 停靠点，方向键在按钮间移动
  function moveToolbarFocus(bar,current,key){
    const buttons=[...bar.querySelectorAll('.latex-tb-btn')];
    const index=buttons.indexOf(current);
    const next={ArrowRight:index+1,ArrowLeft:index-1,Home:0,End:buttons.length-1}[key];
    if(next==null||!buttons.length)return false;
    const target=buttons[(next+buttons.length)%buttons.length];
    buttons.forEach(button=>button.setAttribute('tabindex',button===target?'0':'-1'));
    target.focus();
    return true;
  }

  function syncTopbarHeight(){
    const offset=document.querySelector('.topbar')?.getBoundingClientRect().height||0;
    document.documentElement.style.setProperty('--topbar-height',`${offset}px`);
  }

  function bindToolbars(){
    const page=document.getElementById('p-add');
    // 按下时不抢输入框焦点，保住选区；键盘操作仍可正常聚焦按钮
    page.addEventListener('mousedown',event=>{
      if(event.target.closest('.latex-tb-btn,.latex-dock-body button,.latex-wrap-pref button'))event.preventDefault();
    });
    page.addEventListener('click',event=>{
      const button=event.target.closest('.latex-tb-btn');
      if(!button)return;
      const bar=button.closest('[data-latex-toolbar]');
      const area=document.getElementById(bar.dataset.latexToolbar);
      setTarget(area.id);
      if(button.dataset.latexPop){openPopover(button,event.detail===0);return;}
      closePopover();
      if(button.hasAttribute('data-latex-dock-toggle')){setDockOpen(!dockOpen(),{focusSearch:isDocked()});return;}
      if(button.dataset.latexDelim){applyInsertion(area,{ph:'x',wrap:button.dataset.latexDelim});return;}
      const tool=toolById.get(button.dataset.latexTool);
      if(tool?.ins)applyInsertion(area,tool.ins);
    });
    page.addEventListener('keydown',event=>{
      const button=event.target.closest?.('.latex-tb-btn');
      if(button&&moveToolbarFocus(button.closest('[data-latex-toolbar]'),button,event.key))event.preventDefault();
    });
    for(const id of ['s-problem','s-note'])document.getElementById(id)?.addEventListener('focus',()=>setTarget(id));
  }

  function bindPopover(){
    // 文档级监听的目标可能是 document 本身，用可选调用
    document.addEventListener('mousedown',event=>{
      if(event.target.closest?.('.latex-pop')){event.preventDefault();return;}
      if(popover&&!event.target.closest?.('[data-latex-pop]'))closePopover();
    });
    document.addEventListener('click',event=>{
      const choice=event.target.closest?.('.latex-pop [data-latex-cmd]');
      if(choice)insertCommand(document.getElementById(choice.closest('.latex-pop').dataset.latexTarget),choice.dataset.latexCmd);
    });
    document.addEventListener('mouseover',event=>{
      const choice=event.target.closest?.('.latex-pop [data-latex-cmd]');
      if(choice)choice.closest('.latex-pop').querySelector('[data-latex-pop-code]').textContent=choice.dataset.latexCmd;
    });
    window.addEventListener('resize',positionPopover);
    document.querySelector('.main')?.addEventListener('scroll',()=>{closePopover();hidePeek();},{passive:true});
  }

  function bindDock(){
    const panel=dock(),body=document.getElementById('latex-dock-body');
    document.querySelectorAll('[data-latex-dock-close]').forEach(button=>button.addEventListener('click',()=>{
      setDockOpen(false);targetArea()?.focus({preventScroll:true});
    }));
    document.getElementById('latex-dock-cats')?.addEventListener('click',event=>{
      const button=event.target.closest('[data-latex-category]');
      if(button)setCategory(button.dataset.latexCategory);
    });
    document.getElementById('latex-dock-search')?.addEventListener('input',event=>{state.query=event.target.value;renderDock();});
    document.getElementById('latex-dock-search')?.addEventListener('keydown',event=>{
      if(event.key==='ArrowDown'){event.preventDefault();body.querySelector('.latex-item-main,.latex-glyph')?.focus();}
    });
    document.getElementById('latex-wrap-pref')?.addEventListener('click',event=>{
      const button=event.target.closest('[data-latex-wrap]');
      if(button)setWrap(button.dataset.latexWrap);
    });
    body.addEventListener('click',event=>{
      const copy=event.target.closest('[data-latex-copy]');
      if(copy){void copySource(byId.get(copy.dataset.latexCopy),copy);return;}
      const insert=event.target.closest('[data-latex-insert]');
      if(insert){
        const item=byId.get(insert.dataset.latexInsert);
        applyInsertion(targetArea(),item.wrap?{ph:item.src,wrap:item.wrap}:item.ins?{...item.ins,block:item.block}:{before:item.src,block:item.block},item.id);
        const row=insert.closest('.latex-item');
        row.classList.remove('is-flash');void row.offsetWidth;row.classList.add('is-flash');
        return;
      }
      const glyph=event.target.closest('[data-latex-cmd]');
      if(glyph){insertCommand(targetArea(),glyph.dataset.latexCmd);return;}
      const env=event.target.closest('[data-latex-env]');
      if(env){state.matrix.env=env.dataset.latexEnv;body.querySelectorAll('[data-latex-env]').forEach(button=>{const on=button===env;button.classList.toggle('on',on);button.setAttribute('aria-pressed',String(on));});return;}
      const size=event.target.closest('[data-latex-size]');
      if(size){
        const [rows,cols]=size.dataset.latexSize.split('x').map(Number);
        state.matrix.rows=rows;state.matrix.cols=cols;
        applyInsertion(targetArea(),matrixInsertion(rows,cols,state.matrix.env));
      }
    });
    body.addEventListener('mouseover',event=>{
      const size=event.target.closest('[data-latex-size]');
      if(size){const [rows,cols]=size.dataset.latexSize.split('x').map(Number);paintMatrix(rows,cols);}
      showPeek(event.target.closest('.latex-item'));
    });
    body.addEventListener('mouseout',event=>{
      if(event.target.closest('[data-latex-size-grid]')&&!event.relatedTarget?.closest?.('[data-latex-size-grid]'))paintMatrix(state.matrix.rows,state.matrix.cols);
    });
    body.addEventListener('mouseleave',hidePeek);
    body.addEventListener('focusin',event=>showPeek(event.target.closest('.latex-item')));
    body.addEventListener('focusout',hidePeek);
    body.addEventListener('scroll',hidePeek,{passive:true});
    body.addEventListener('keydown',event=>{
      if(!['ArrowDown','ArrowUp'].includes(event.key))return;
      const items=[...body.querySelectorAll('.latex-item-main,.latex-glyph')];
      const index=items.indexOf(document.activeElement);
      const next=items[index+(event.key==='ArrowDown'?1:-1)];
      if(next){event.preventDefault();next.focus();}
      else if(event.key==='ArrowUp'&&index===0){event.preventDefault();document.getElementById('latex-dock-search')?.focus();}
    });
    panel.addEventListener('animationend',()=>panel.classList.remove('is-opening'));
  }

  function bindShortcuts(){
    document.addEventListener('keydown',event=>{
      const onPage=document.getElementById('p-add')?.classList.contains('on');
      if(onPage&&(event.ctrlKey||event.metaKey)&&event.key==='/'){
        event.preventDefault();
        setDockOpen(!dockOpen(),{focusSearch:true});
        if(!dockOpen())targetArea()?.focus({preventScroll:true});
        return;
      }
      if(event.key!=='Escape')return;
      if(popover){closePopover({restoreFocus:popover.contains(document.activeElement)});return;}
      if(dockOpen()&&(document.activeElement?.closest?.('#latex-dock')||!isDocked())){
        setDockOpen(false);targetArea()?.focus({preventScroll:true});
      }
    });
  }

  function restorePreferences(){
    const wrap=readStorage(STORAGE.wrap,'dollar');
    state.wrap=DELIMITERS[wrap]?wrap:'dollar';
    try{
      const recent=JSON.parse(readStorage(STORAGE.recent,'[]'));
      state.recent=Array.isArray(recent)?recent.filter(id=>byId.has(id)).slice(0,RECENT_LIMIT):[];
    }catch{state.recent=[];}
    const category=readStorage(STORAGE.category,'');
    state.category=categories.includes(category)?category:(state.recent.length?'最近使用':'全部');
  }

  function bind(){
    if(typeof document.querySelector!=='function'||!document.getElementById('latex-dock'))return;
    restorePreferences();
    renderToolbars();
    setWrap(state.wrap);
    setTarget(state.targetId);
    bindToolbars();bindPopover();bindDock();bindShortcuts();
    syncDockMode();syncTopbarHeight();
    // 页面切回录入时重排公式（隐藏面板里的渲染会被跳过）；离开时收起浮层
    window.addEventListener('cuoti:route-active',event=>{
      if(event.detail?.route!=='add'){closePopover();hidePeek();return;}
      syncTopbarHeight();renderDock();
    });
    let resizeTimer=0;
    window.addEventListener('resize',()=>{
      syncTopbarHeight();
      window.clearTimeout(resizeTimer);
      resizeTimer=window.setTimeout(()=>{syncDockMode();fitFormulas(document.getElementById('latex-dock-body'));},120);
    });
  }

  window.Cuoti=window.Cuoti||{};
  window.Cuoti.latexGuide=Object.freeze({
    catalog,categories,symbols,toolbar,delimiters:DELIMITERS,
    mathContextAt,delimitersFor,planInsertion,commandInsertion,matrixInsertion,filterCatalog,filterSymbols,
  });
  bind();
})();
