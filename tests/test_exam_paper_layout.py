import pytest

from cuoti_app.services.exam_paper_layout import question_display_content, render_question_content
from cuoti_app.services.reviewed_source_labels import REVIEWED_SOURCE_LABELS


@pytest.mark.parametrize('candidate_id,label', REVIEWED_SOURCE_LABELS.items())
def test_reviewed_labels_are_literal_candidate_bound_and_idempotent(candidate_id, label):
    prefix, stem = label
    remainder = stem + ' END'
    original = prefix + remainder
    assert question_display_content(original, candidate_id=candidate_id) == remainder
    assert question_display_content(remainder, candidate_id=candidate_id) == remainder
    assert question_display_content(original, candidate_id='unreviewed') == original
    changed = prefix + 'CHANGED ' + remainder
    assert question_display_content(changed, candidate_id=candidate_id) == changed
    assert render_question_content(original, candidate_id=candidate_id) == render_question_content(remainder)


def test_strips_confirmed_source_labels_without_touching_source_context():
    assert render_question_content("【例 5．17】（华中师范大学，1991年）设 x=1。") == (
        "（华中师范大学，1991年）设 x=1。"
    )
    assert render_question_content("37．（清华大学，2002 年）设 A=0。") == (
        "（清华大学，2002 年）设 A=0。"
    )
    assert render_question_content("37．设 A=0。") == "37．设 A=0。"
    assert render_question_content("37．（2002年）是一个数学标记。") == (
        "37．（2002年）是一个数学标记。"
    )


@pytest.mark.parametrize('candidate_id', [
    'QB52D4323016A58F659AEB5504', 'QBD6EC10793D5A55269513CFD8',
])
def test_source_exclusion_boundary_heading_is_display_only_and_candidate_bound(candidate_id):
    original = r"设函数 \( u=u(x, y, z) \) 满足原条件。" + '\n单元练习7．5'
    expected = original.rsplit('\n', 1)[0]
    assert question_display_content(original, candidate_id=candidate_id) == expected
    assert question_display_content(expected, candidate_id=candidate_id) == expected
    assert question_display_content(original, candidate_id='other') == original
    changed = original.replace('设函数', '另一个题目')
    assert question_display_content(changed, candidate_id=candidate_id) == changed
    inline = original.replace('\n', ' ')
    assert question_display_content(inline, candidate_id=candidate_id) == inline


def test_strips_only_the_confirmed_bare_source_number_fingerprint():
    confirmed = (
        r"80．已知 4 维线性空间 \( V \) 的线性变换 \( \sigma \) "
        r"在一个基下的矩阵为 \( A \)．"
    )
    assert render_question_content(confirmed) == confirmed.removeprefix("80．")
    assert render_question_content(r"80．已知 4 维线性空间 \( W \) 的性质。") == (
        r"80．已知 4 维线性空间 \( W \) 的性质。"
    )
    assert render_question_content(r"80．设 \( V \) 是 4 维线性空间。") == (
        r"80．设 \( V \) 是 4 维线性空间。"
    )


def test_strips_exam71_bare_numbers_only_for_verified_candidates_and_stems():
    source_18 = (
        r"18．设 \( n \) 阶方阵 \( A \) 和 \( B \) 有相同的特征多项式"
        r"及最小多项式，问 \( A \) 与 \( B \) 是否相似？"
    )
    source_41 = (
        r"41．设 \( \sigma \) 是2维欧氏空间 \( V \) 的一个旋转，"
        r"将 \( \sigma \) 表示成2个镜面反射的乘积。"
    )
    assert render_question_content(
        source_18, candidate_id="QB0E0444995F25ECFC31F4029D"
    ) == source_18.removeprefix("18．")
    assert render_question_content(
        source_41, candidate_id="QB329994F26723BC33C6E1D063"
    ) == source_41.removeprefix("41．")
    assert render_question_content(source_18) == source_18
    assert render_question_content(
        source_18, candidate_id="QB329994F26723BC33C6E1D063"
    ) == source_18
    assert render_question_content(
        r"18．设 \( n \) 阶方阵 \( X \) 的性质。",
        candidate_id="QB0E0444995F25ECFC31F4029D",
    ) == r"18．设 \( n \) 阶方阵 \( X \) 的性质。"


def test_reviewed_batch002_source_numbers_are_not_repeated_in_the_paper():
    cases = [
        ('QB92C0711EA623DB384B4BDFFB', r'22．设 \( A, B \) 都是矩阵，证明两个解空间等价。', '22．'),
        ('QB47D2E658424EFF208E9B2BA9', '5．由紧性定理证明聚点定理。', '5．'),
    ]
    for candidate_id, content, prefix in cases:
        result = render_question_content(content, candidate_id=candidate_id)
        assert result == content.removeprefix(prefix)
        assert render_question_content(content, candidate_id='unknown') == content
        assert render_question_content(result, candidate_id=candidate_id) == result
    assert render_question_content('5．是一个需要保留的数值。', candidate_id=cases[1][0]) == '5．是一个需要保留的数值。'


def _direct_product_question():
    return (
        r"【例 5．4】设 \( V_{1}, V_{2} \) 是数域 \( P \) 上的线性空间，规定 "
        r"\( \left(\boldsymbol{\alpha}_{1}, \boldsymbol{\alpha}_{2}\right)"
        r"+\left(\boldsymbol{\beta}_{1}, \boldsymbol{\beta}_{2}\right)= "
        r"\left(\boldsymbol{\alpha}_{1}+\boldsymbol{\beta}_{1}, "
        r"\boldsymbol{\alpha}_{2}+\boldsymbol{\beta}_{2}\right) \) ，（1）"
        r"\( k\left(\boldsymbol{\alpha}_{1}, \boldsymbol{\alpha}_{2}\right)="
        r"\left(k \boldsymbol{\alpha}_{1}, k \boldsymbol{\alpha}_{2}\right) \) 。"
        r"（2）（1）证明：\( V_{1}\times V_{2} \) 是线性空间；"
        "\n\n（2）求它的维数。"
    )


def test_exam71_direct_product_keeps_definition_tags_and_separates_questions():
    content = _direct_product_question()
    rendered = render_question_content(
        content, candidate_id="QB2C9BE69EFFDCAE915841983C"
    )
    assert "（2）（1）" not in rendered
    assert rendered.count(r"\tag{1}") == 1
    assert rendered.count(r"\tag{2}") == 1
    assert rendered.count(r"\par\smallskip\noindent") == 2
    for math_body in (
        r"\left(\boldsymbol{\alpha}_{1}, \boldsymbol{\alpha}_{2}\right)"
        r"+\left(\boldsymbol{\beta}_{1}, \boldsymbol{\beta}_{2}\right)= "
        r"\left(\boldsymbol{\alpha}_{1}+\boldsymbol{\beta}_{1}, "
        r"\boldsymbol{\alpha}_{2}+\boldsymbol{\beta}_{2}\right)",
        r"k\left(\boldsymbol{\alpha}_{1}, \boldsymbol{\alpha}_{2}\right)="
        r"\left(k \boldsymbol{\alpha}_{1}, k \boldsymbol{\alpha}_{2}\right)",
    ):
        assert math_body in rendered
    assert render_question_content(
        rendered, candidate_id="QB2C9BE69EFFDCAE915841983C"
    ) == rendered


def test_direct_product_marker_is_candidate_bound_and_tex_protected():
    content = _direct_product_question()
    assert render_question_content(content) != render_question_content(
        content, candidate_id="QB2C9BE69EFFDCAE915841983C"
    )
    assert render_question_content(
        content, candidate_id="QB-UNKNOWN"
    ) == render_question_content(content)
    fragment = content.removeprefix("【例 5．4】")
    for protected in (
        "% " + fragment.replace("\n", " "),
        r"\verb|" + fragment.replace("\n", " ") + "|",
        "$" + fragment.replace("\n", " ") + "$",
    ):
        assert render_question_content(
            protected, candidate_id="QB2C9BE69EFFDCAE915841983C"
        ) == protected


def test_strips_only_a_standalone_trailing_exercise_heading():
    assert render_question_content("题干。\n\n\\textbf{习题 3.2}") == "题干。"
    assert render_question_content("阅读\\textbf{习题 3.2}并作答。") == (
        "阅读\\textbf{习题 3.2}并作答。"
    )
    assert render_question_content("请阅读\\textbf{习题 3.2}") == (
        "请阅读\\textbf{习题 3.2}"
    )


def test_lays_out_a_trusted_continuous_subquestion_sequence():
    rendered = render_question_content("题干。（1）第一问；（2）第二问；（3）第三问。")
    assert rendered == (
        "题干。\n\\par\\smallskip\\noindent （1）第一问；"
        "\n\\par\\smallskip\\noindent （2）第二问；"
        "\n\\par\\smallskip\\noindent （3）第三问。"
    )


def test_does_not_rewrite_references_math_or_macro_arguments():
    content = (
        "由（1）可知（2）。 "
        r"\(（1）+（2）\) "
        r"\begin{matrix}（1）&（2）\end{matrix} "
        r"\text{（1）与（2）}"
    )
    assert render_question_content(content) == content


def test_math_comments_and_macro_arguments_are_protected():
    for content in (
        r"$（1）+（2）$",
        "\\textbf{（1）与（2）}",
        "% （1）与（2）是注释\n题干。",
    ):
        assert render_question_content(content) == content


def test_escaped_dollar_does_not_hide_real_subquestions():
    content = r"费用\$5。（1）第一问；（2）第二问。"
    rendered = render_question_content(content)
    assert rendered.count(r"\par\smallskip\noindent") == 2


def test_layout_is_idempotent():
    content = "【例 3.2】题干。（1）第一问；（2）第二问。\n\\textbf{习题 3.2}"
    rendered = render_question_content(content)
    assert render_question_content(rendered) == rendered


def test_reviewed_field_condition_wrap_is_scoped_and_preserves_math():
    candidate_id = "QBAC330D3A2A06CE5AA2728C35"
    row = r"0 \leq i \leq n, 0 \leq j \leq m,\quad b_{0}+b_{1}e+\cdots+b_{m}e^{m}\neq 0"
    content = "令\n\\[\nF=\\left\\{\\begin{array}{l}\n" + row + "\n\\end{array}\\right\\}\n\\]"
    expected = content.replace(r",\quad b_{0}", ",\\\\\nb_{0}")
    assert question_display_content(content, candidate_id=candidate_id) == expected
    assert render_question_content(content, candidate_id=candidate_id) == expected
    assert question_display_content(expected, candidate_id=candidate_id) == expected
    assert render_question_content(expected, candidate_id=candidate_id) == expected
    assert question_display_content(content, candidate_id="unknown") == content
    assert question_display_content(content.replace("F=", "G="), candidate_id=candidate_id) == content.replace("F=", "G=")
    assert question_display_content(content.replace("e^{m}", "e^{k}"), candidate_id=candidate_id) == content.replace("e^{m}", "e^{k}")


def test_does_not_rewrite_incomplete_or_nonconsecutive_markers():
    content = "题干。（1）第一部分；（3）第三部分。"
    assert render_question_content(content) == content


def test_allows_long_inline_products_to_wrap_without_changing_the_math():
    content = (
        r"计算 \( \left(c_1+c_2\right)\left(c_2+c_3\right)"
        r"\left(c_3+c_1\right) \)．"
    )
    rendered = render_question_content(content)
    assert rendered == (
        r"计算 \( \left(c_1+c_2\right)\allowbreak\left(c_2+c_3\right)"
        r"\allowbreak\left(c_3+c_1\right) \)．"
    )
    assert rendered.replace(r"\allowbreak", "") == content
    assert render_question_content(rendered) == rendered


def test_does_not_add_wraps_outside_inline_math():
    content = r"说明命令 \right)\left( 的文本，并保留 \[a\right)\left(b\]。"
    assert render_question_content(content) == content


def test_inline_product_wraps_preserve_literal_commands_and_comments():
    content = (
        r"\verb|\(a\right)\left(b\)|" + "\n"
        r"% \(a\right)\left(b\)" + "\n"
        r"\\(a\right)\left(b\)"
    )
    assert render_question_content(content) == content
