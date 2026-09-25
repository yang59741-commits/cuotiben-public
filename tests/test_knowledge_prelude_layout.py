import pytest

from cuoti_app.services.exam_paper_layout import knowledge_prelude_latex
from cuoti_app.services.exam_tex import assemble_exam_document


@pytest.mark.parametrize('source', [
    '1. 第一定义。2. 第二定理。3. 第三公式。',
    '1. 第一定义。\n2. 第二定理。\n3. 第三公式。',
    '（1）第一定义。（2）第二定理。（3）第三公式。',
    '(1) 第一定义； (2) 第二定理； (3) 第三公式。',
])
def test_numbered_prelude_becomes_separate_list_items(source):
    result = knowledge_prelude_latex(source)
    assert result.startswith(r'\begin{enumerate}')
    assert result.count(r'\item ') == 3
    assert knowledge_prelude_latex(result) == result


def test_math_decimals_and_nested_tex_are_not_numbered_points():
    source = r'1. 条件 $x=1.25, (2)\in A$，并且 \(f(2)=3.5\)。2. 定理 \textbf{版本（3）}。'
    result = knowledge_prelude_latex(source)
    assert result.count(r'\item ') == 2
    assert r'$x=1.25, (2)\in A$' in result
    assert r'\(f(2)=3.5\)' in result
    assert r'\textbf{版本（3）}' in result


def test_existing_lists_and_unstructured_text_are_preserved():
    for source in (r'\begin{itemize}\item 定义\item 定理\end{itemize}',
                   '这是定义。这里没有明确分点。', '定理1.1 与定理2.1。'):
        assert knowledge_prelude_latex(source) == source


def test_document_formats_only_prelude_not_problem_numbers():
    question = r'\begin{problem}（1）证明结论。（2）计算。\end{problem}'
    body = r'\section*{基础知识预备}1. 定义。2. 定理。' + question
    document = assemble_exam_document('排版检查', '小样本', body)
    assert document.count(r'\item ') == 2
    assert question in document
