"""Static learning content and chapter-to-TeX mappings."""

MATH_WORDS = [
    ("极限","limit——描述变量在无限变化中趋向某一确定值的趋势"),
    ("连续","continuous——函数在该点有定义、极限存在且等于函数值"),
    ("导数","derivative——函数增量与自变量增量比值的极限"),
    ("微分","differential——函数增量的线性主部"),
    ("积分","integral——和的极限，黎曼和的极限过程"),
    ("级数","series——无穷多项之和，其敛散性是核心问题"),
    ("特征值","eigenvalue——线性变换作用下保持方向不变的特殊标量"),
    ("正交","orthogonal——内积为零的向量，推广了垂直概念"),
    ("同构","isomorphism——保持代数结构的双射，揭示空间本质相同"),
    ("行列式","determinant——方阵对应的标量，刻画体积缩放与可逆性"),
    ("秩","rank——矩阵行/列向量组的极大无关组所含向量个数"),
    ("相似","similar——存在可逆矩阵P使B=P⁻¹AP，共享特征值"),
    ("合同","congruent——存在可逆矩阵P使B=P'AP，保持二次型类型"),
    ("可微","differentiable——函数局部可被线性映射逼近"),
    ("一致收敛","uniform convergence——对区间上所有点用同一个N控制收敛"),
    ("紧集","compact set——任意开覆盖有有限子覆盖/有界闭集"),
    ("范数","norm——向量\"长度\"的公理化推广"),
    ("对偶空间","dual space——线性空间上所有线性泛函构成的空间"),
]

QUOTES = [
    "数学是上帝书写宇宙的语言。——伽利略",
    "一门科学只有当它成功地运用数学时，才算达到了真正完善的地步。——马克思",
    "纯粹数学，就其本质而言，是逻辑思想的诗篇。——爱因斯坦",
    "数学的本质在于它的自由。——康托尔",
    "我们必须知道，我们必将知道。——希尔伯特",
    "数学是无限的科学。——外尔",
    "哪里有数，哪里就有美。——普罗克洛斯",
    "数学是所有科学的大门和钥匙。——培根",
    "在数学中，我们发现的不是真理，而是理解。——高斯",
    "数学是符号逻辑加上精确的判断。——庞加莱",
    "做数学不需要聪明，需要坚持。——陶哲轩",
    "数学的严密性来自对每一个细节的审视。——柯西",
    "分析学是无穷的交响乐。——欧拉",
    "好的数学问题比好的解答更有价值。——康托洛维奇",
]

def extract_problems_from_tex(filepath, count=3):
    """从试题库tex文件中提取参考题目"""
    import re, random as rnd
    from .utils import QUESTION_SUBSECTION_SPLIT_PATTERN
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except (OSError, UnicodeError):
        return []
    # Remove preamble/comments
    content = re.sub(r'(?<!\\)%.*', '', content)
    # Split by subsection{题目N} pattern
    parts = QUESTION_SUBSECTION_SPLIT_PATTERN.split(content)
    if len(parts) < 2:
        # Try alternative: split by numbered items at line start
        parts = re.split(r'\n(?=\d+[．.])', content)
    problems = []
    for p in parts[1:]:
        p = p.strip()
        if len(p) > 30:  # skip trivial fragments
            # Limit each problem to ~500 chars
            problems.append(p[:600])
    if len(problems) <= count:
        return problems
    return rnd.sample(problems, count)
