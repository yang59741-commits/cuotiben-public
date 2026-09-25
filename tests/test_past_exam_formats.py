import tempfile
import unittest
from pathlib import Path

from tools.import_past_exams import _supplement_sections
from cuoti_app.services.past_exams import (
    PastExamSourceError,
    _main_question_anchors,
    _list_fragment_issues,
    build_exam_body,
    build_other_past_candidate_pool,
    discover_past_exam_sources,
    extract_yearly_problems,
    learning_sample_sections,
    other_past_candidates_by_ids,
    past_exam_source_snapshot,
    public_source,
    reference_exam_structure,
    _structure_warnings,
)


class StandalonePastExamFormatTests(unittest.TestCase):
    def test_short_scored_question_group_keeps_its_numbered_children(self):
        for marked in (False, True):
            with self.subTest(marked=marked):
                body = '\n'.join((
                    '一．（14分）求给定向量组线性无关的参数取值。',
                    r'二．（21分）解答如下问题：\\',
                    r'\subsection{题目1}' if marked else '',
                    r'1．（7分）给定对角矩阵 \(A\)，求与其可交换的所有方阵。',
                    r'\subsection{题目2}' if marked else '',
                    r'2．（14分）证明满足给定条件的矩阵 \(A,B\) 可同时对角化。',
                    '三．（15分）求给定线性方程组有解的参数条件。',
                ))
                anchors = _main_question_anchors(body)
                self.assertEqual([a['raw_number'] for a in anchors], ['一', '二', '三'])
                problems = extract_yearly_problems({'text': body})
                self.assertEqual(len(problems), 3)
                self.assertIn('可交换', problems[1]['content'])
                self.assertIn('可同时对角化', problems[1]['content'])
                self.assertNotIn('线性方程组', problems[1]['content'])

    def test_discussion_instructions_are_section_metadata_not_a_missing_question(self):
        heading = '二．辨析题．正确的给出理由，错误的举出反例或说明理由．每题6分，共12分．'
        body = '\n'.join(('一．填空题．每题5分，共5分．', r'\subsection{题目1}',
                          r'1．求矩阵的秩 \(\_\_\_\_\)。', heading,
                          r'\subsection{题目2}', '1．判断给定线性变换是否单射。',
                          r'\subsection{题目3}', '2．判断给定矩阵是否可对角化。'))
        problems = extract_yearly_problems({'text': body})
        self.assertEqual(len(problems), 3)
        self.assertFalse(any('正确的给出理由' in p['content'] for p in problems))
        self.assertEqual([a['section'] for a in _main_question_anchors(body)[1:]], [heading] * 2)

    def test_consecutive_chinese_main_survives_child_list_wrapper(self):
        for prior_children in ('', r'\item[（1）] 证明上一题的结论。'):
            with self.subTest(prior_children=prior_children):
                body = '\n'.join(('六．证明给定函数在闭区间上一致收敛。',
                    r'\begin{itemize}', prior_children,
                    r'\item[七．] 设数列满足给定递推关系。',
                    r'\item[（1）] 证明数列收敛。', r'\item[（2）] 求数列极限。',
                    r'\end{itemize}', '八．求给定二元函数的极值。'))
                problems = extract_yearly_problems({'text': body})
                self.assertEqual(len(problems), 3)
                self.assertNotIn('递推关系', problems[0]['content'])
                self.assertIn('递推关系', problems[1]['content'])
                self.assertTrue(problems[1]['content'].startswith('设数列'))
                self.assertIn('求数列极限', problems[1]['content'])
                self.assertFalse(_list_fragment_issues(problems[1]['content']))
        child = '\n'.join(('六．证明以下两个结论。', r'\begin{itemize}',
                           r'\item[一．] 第一个结论。', r'\item[二．] 第二个结论。',
                           r'\end{itemize}'))
        self.assertEqual(len(extract_yearly_problems({'text': child})), 1)

    def test_numbered_score_only_partition_is_not_a_question(self):
        heading = r'七．（第1，2题15分，第3题20分）\\'
        body = '\n'.join(('一．（20分）计算给定矩阵的行列式。', heading,
                          r'\subsection{题目1}', '1．证明给定多项式不可约。',
                          r'\subsection{题目2}', '2．证明给定矩阵可逆。',
                          r'\subsection{题目3}', '3．求给定线性空间的一组基。'))
        problems = extract_yearly_problems({'text': body})
        self.assertEqual(len(problems), 4)
        self.assertFalse(any('第1，2题15分' in p['content'] for p in problems))
        self.assertEqual([a['section'] for a in _main_question_anchors(body)[1:]], [heading.rstrip('\\')] * 3)
        substantive = '一．（第1，2题15分，第3题20分）证明下列三个命题。'
        self.assertEqual(len(_main_question_anchors(substantive)), 1)

    def test_scored_chinese_main_keeps_plain_numbered_children(self):
        body = '\n'.join((
            '一．（15分）设给定矩阵属于有限维线性空间，并满足下列条件。',
            '', '1．求矩阵的全部特征值。', '', '2．证明矩阵可以对角化。',
            '', '二．（10分）计算给定矩阵的行列式并写明步骤。',
            '', '三．计算题。', '', '1．求另一个线性方程组的通解。'))
        anchors = _main_question_anchors(body)
        self.assertEqual([a['raw_number'] for a in anchors], ['一', '二', '1'])
        problems = extract_yearly_problems({'text': body})
        self.assertEqual(len(problems), 3)
        self.assertIn('1．求矩阵的全部特征值', problems[0]['content'])
        self.assertIn('2．证明矩阵可以对角化', problems[0]['content'])

    def test_combined_calculation_proof_heading_is_not_an_extra_question(self):
        for heading in ('二．计算证明题．每题15分，共30分．',
                        '二．计算与证明．每题15分，共30分．'):
            with self.subTest(heading=heading):
                body = '\n'.join((r'一．填空题．', r'\subsection{题目1}',
                                  r'1．求矩阵的秩 \(\_\_\_\_\)。', heading,
                                  r'\subsection{题目2}', '1．计算矩阵的全部特征值。',
                                  r'\subsection{题目3}', '2．证明给定线性映射为单射。'))
                anchors = _main_question_anchors(body)
                self.assertEqual([a['number'] for a in anchors], ['1', '2', '3'])
                self.assertEqual([a['section'] for a in anchors[1:]], [heading, heading])
                problems = extract_yearly_problems({'text': body})
                self.assertEqual(len(problems), 3)
                self.assertFalse(any('每题15分' in p['content'] for p in problems))

    def test_empty_duplicate_heading_retains_outer_number_and_original_body(self):
        body = r"""
\subsection{题目1}
1. 求给定函数的导数及其零点。
\subsection{题目2}
\end{itemize}
\subsection{题目1}
原题边界“1．”：判断给定函数的凸性。
\subsection{题目3}
\subsection{题目2}
原题边界“2．”：计算给定函数的积分。
"""
        anchors = _main_question_anchors(body)
        self.assertEqual([a['number'] for a in anchors], ['1', '2', '3'])
        self.assertEqual([a['raw_number'] for a in anchors], ['1', '1', '2'])
        problems = extract_yearly_problems({'text': body})
        self.assertIn('原题边界“1．”', problems[1]['content'])

    def test_original_chinese_sequence_disambiguates_repeated_imported_numbers(self):
        body = r"""
\subsection{题目1}
一．计算给定矩阵的行列式。
\subsection{题目2}
原题边界“二．”：判断给定的命题并说明理由。
\subsection{题目2}
三．求给定矩阵的全部特征值。
\subsection{题目3}
四．证明给定矩阵满足多项式恒等式。
"""
        self.assertEqual([a['number'] for a in _main_question_anchors(body)], ['1', '2', '3', '4'])

    def test_empty_answer_before_math_closer_stays_in_fill_blank_section(self):
        body = r"""
一．填空题。
\subsection{题目1}
1. 设A是给定方阵，求其行列式 \(D=\)。
\subsection{题目2}
2. 求给定矩阵的全部特征值 \(\_\_\_\_\)。
二．证明题。
\subsection{题目3}
1. 证明给定的矩阵可以对角化。
"""
        anchors = _main_question_anchors(body)
        self.assertEqual([a['section'] for a in anchors], ['一．填空题。', '一．填空题。', '二．证明题。'])
        problems = extract_yearly_problems({'text': body})
        self.assertEqual(problems[0]['source_section'], '一．填空题。')

    def test_marked_main_questions_in_outer_lists_after_complete_questions(self):
        body = r"""
\subsection{题目1}
1. 求函数在零点处的导数。
\begin{itemize}
\subsection{题目2}
\item[2．] 求函数在闭区间上的最大值。
\subsection{题目3}
\item[3．] 计算给定函数的定积分。
\end{itemize}
\subsection{题目4}
1. 证明连续函数在闭区间上有界。
\begin{itemize}
\subsection{题目5}
\item[2．] 证明给定函数一致连续。
\end{itemize}
\begin{itemize}
\subsection{题目6}
\item[1．] 判断给定级数是否绝对收敛。
\end{itemize}
"""
        anchors = _main_question_anchors(body)
        self.assertEqual([a["number"] for a in anchors], [str(n) for n in range(1, 7)])
        self.assertEqual([a["raw_number"] for a in anchors], ["1", "2", "3", "1", "2", "1"])

    def test_markers_inside_shared_stem_plain_items_remain_subquestions(self):
        body = r"""
\subsection{题目1}
1. 设函数在闭区间上连续，证明下列结论：
\begin{itemize}
\subsection{题目2}
\item[1．] 函数在闭区间上有界。
\subsection{题目3}
\item[2．] 函数在闭区间上取得最大值。
\end{itemize}
\subsection{题目4}
2. 计算给定函数的定积分。
"""
        self.assertEqual([a["number"] for a in _main_question_anchors(body)], ["1", "4"])

    def test_parenthesized_marked_items_remain_subquestions(self):
        body = r"""
\subsection{题目1}
1. 设函数在闭区间上连续。
\begin{itemize}
\subsection{题目2}
\item[（1）] 证明函数有界。
\subsection{题目3}
\item[（2）] 证明函数取得最大值。
\end{itemize}
"""
        self.assertEqual([a["number"] for a in _main_question_anchors(body)], ["1"])

    def test_filename_year_and_no_space_numbering_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "测试大学" / "数学分析"
            root.mkdir(parents=True)
            (root / "2026年.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\n"
                "\\section*{测试大学2026年数学分析试卷}\n"
                "一、求极限。\n二．证明结论。\n\\end{document}\n",
                encoding="utf-8",
            )
            source = discover_past_exam_sources(str(Path(temporary)))
            self.assertEqual(len(source), 1)
            self.assertEqual(source[0]["latest_year"], 2026)
            self.assertEqual(source[0]["question_count"], 2)

    def test_learning_years_are_deterministic_and_bounded_by_reference_year(self):
        short = {"exam_sections": [{"year": year} for year in (2024, 2025)]}
        samples, mode = learning_sample_sections(short)
        self.assertEqual([item["year"] for item in samples], [2024, 2025])
        self.assertEqual(mode, "all")

        long = {"exam_sections": [{"year": year} for year in range(2021, 2027)]}
        samples, mode = learning_sample_sections(long)
        self.assertEqual([item["year"] for item in samples], [2024, 2025, 2026])
        self.assertEqual(mode, "deterministic")
        earlier, _ = learning_sample_sections(long, reference_year=2025)
        self.assertEqual([item["year"] for item in earlier], [2023, 2024, 2025])
        self.assertEqual(reference_exam_structure(
            {
                "exam_sections": [
                    {"year": 2025, "text": r"\subsection{题目1} 足够长的测试题目正文。"}
                ]
            },
            2025,
        )["reference_year"], 2025)

    def test_combined_dalian_sections_keep_non_year_headings_and_merge_same_year(self):
        text = r"""
\begin{document}
\section*{大连理工大学2005硕士研究生入学考试数学分析试题}
\section*{一、计算题}
\begin{itemize}
\item[（1．] 求极限。
\end{itemize}
\section*{2020年补四道题：}
补充题。
\section*{大连理工大学2020年研究生入学考试数学分析试题}
正式题。
\end{document}
""".strip()
        sections = _supplement_sections(text)
        self.assertEqual([section["year"] for section in sections], [2005, 2020, 2020])
        self.assertIn("一、计算题", sections[0]["text"])

    def test_normalized_subsections_win_over_nested_body_numbering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "大连理工大学" / "数学分析"
            root.mkdir(parents=True)
            (root / "2026年.tex").write_text(
                r"""
\begin{document}
\section*{大连理工大学2026年数学分析真题}
\noindent\textbf{一、计算题}
\subsection{题目1}
1. 这是题目内部的第一小问，不应被识别为主问题。
2. 这是题目内部的第二小问，也不应被识别为主问题。
\subsection{题目2}
（1）证明第一个结论。
（2）证明第二个结论。
\end{document}
""".strip(),
                encoding="utf-8",
            )
            source = discover_past_exam_sources(temporary)[0]
            self.assertEqual(source["question_count"], 2)
            self.assertEqual(source["question_numbers"], [1, 2])
            public = public_source(source, 1)
            self.assertEqual(public["year_structures"][0]["question_count"], 2)
            self.assertTrue(public["year_structures"][0]["available"])

    def test_section_star_and_chinese_item_labels_form_structure_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "测试大学" / "高等代数"
            root.mkdir(parents=True)
            (root / "2026年.tex").write_text(
                r"""
\begin{document}
\section*{一．计算题．}
\subsection{题目1} 第一题。
\subsection{题目2} 第二题。
\section*{二．证明题．}
\subsection{题目3} 第三题。
\item[三．] 综合题．
\subsection{题目4} 第四题。
\end{document}
""".strip(),
                encoding="utf-8",
            )
            source = discover_past_exam_sources(temporary)[0]
            self.assertEqual(
                [item["label"] for item in source["sections"]],
                ["一．计算题．", "二．证明题．", "三．综合题．"],
            )
            self.assertEqual(
                [item["question_count"] for item in source["sections"]],
                [2, 1, 1],
            )

    def test_marked_questions_recover_unnamed_blocks_from_number_resets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "测试大学" / "数学分析"
            root.mkdir(parents=True)
            body = [r"\begin{document}"]
            for marker, source_number in enumerate((1, 2, 3, 1, 2, 1), 1):
                body.extend(
                    (
                        f"\\subsection{{题目{marker}}}",
                        f"{source_number}．第{marker}道主问题。",
                    )
                )
            body.append(r"\end{document}")
            (root / "2026年.tex").write_text("\n".join(body), encoding="utf-8")
            source = discover_past_exam_sources(temporary)[0]
            self.assertEqual(
                [item["question_count"] for item in source["sections"]],
                [3, 2, 1],
            )

    def test_invalid_year_is_exposed_with_warning_but_not_selectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "湖南师范大学" / "高等代数"
            root.mkdir(parents=True)
            (root / "2025年.tex").write_text(
                r"\begin{document}\subsection{题目1} 这是完整且足够长的主问题正文。\end{document}",
                encoding="utf-8",
            )
            (root / "2026年.tex").write_text(
                r"\begin{document}只有说明，没有任何题目标记。\end{document}",
                encoding="utf-8",
            )
            source = discover_past_exam_sources(temporary)[0]
            years = public_source(source, 1)["year_structures"]
            self.assertEqual([item["year"] for item in years], [2026, 2025])
            self.assertFalse(years[0]["available"])
            self.assertIn("无法识别", years[0]["warning"])
            with self.assertRaises(PastExamSourceError):
                reference_exam_structure(source, 2026)

    def test_unusually_short_year_is_selectable_but_warned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "测试理工大学" / "高等代数"
            root.mkdir(parents=True)
            for year, count in ((2024, 10), (2025, 10), (2026, 3)):
                questions = "\n".join(
                    f"\\subsection{{题目{number}}} 第{number}题正文足够长，求证相应结论。"
                    for number in range(1, count + 1)
                )
                (root / f"{year}年.tex").write_text(
                    "\\begin{document}\n" + questions + "\n\\end{document}",
                    encoding="utf-8",
                )
            source = discover_past_exam_sources(temporary)[0]
            years = public_source(source, 1)["year_structures"]
            latest = next(item for item in years if item["year"] == 2026)
            self.assertTrue(latest["available"])
            self.assertFalse(latest["default_eligible"])
            self.assertEqual(latest["question_count"], 3)
            self.assertIn("明显少于历年典型题量", latest["warning"])
            self.assertEqual(source["latest_year"], 2025)

    def test_audited_three_block_dalian_paper_is_not_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "大连理工大学" / "高等代数"
            root.mkdir(parents=True)
            for year, count in ((2020, 13), (2021, 13), (2022, 3)):
                questions = "\n".join(
                    f"\\subsection{{题目{number}}} 第{number}题正文。"
                    for number in range(1, count + 1)
                )
                (root / f"{year}年.tex").write_text(
                    "\\begin{document}\n" + questions + "\n\\end{document}",
                    encoding="utf-8",
                )
            source = discover_past_exam_sources(temporary)[0]
            paper = next(
                item for item in source["all_exam_sections"]
                if item["year"] == 2022
            )
            self.assertEqual(paper["structure"]["question_count"], 3)
            self.assertEqual(paper["warnings"], [])

    def test_audited_missing_source_keeps_warning_and_cannot_be_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "郑州大学" / "高等代数"
            root.mkdir(parents=True)
            for year in (2025, 2026):
                questions = "\n".join(
                    f"\\subsection{{题目{number}}} 第{number}题正文。"
                    for number in range(1, 14)
                )
                (root / f"{year}年.tex").write_text(
                    "\\begin{document}\n" + questions + "\n\\end{document}",
                    encoding="utf-8",
                )
            source = discover_past_exam_sources(temporary)[0]
            paper = next(
                item for item in source["all_exam_sections"]
                if item["year"] == 2026
            )
            self.assertIn("仅提供5题", paper["warning"])
            self.assertEqual(source["latest_year"], 2025)

    def test_audited_zhejiang_fragment_keeps_nearest_complete_year(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "浙江大学" / "高等代数"
            root.mkdir(parents=True)
            for year, count in ((2025, 10), (2026, 5)):
                questions = "\n".join(
                    f"\\subsection{{题目{number}}} 第{number}题完整正文。"
                    for number in range(1, count + 1)
                )
                (root / f"{year}年.tex").write_text(
                    "\\begin{document}\n" + questions + "\n\\end{document}",
                    encoding="utf-8",
                )
            source = discover_past_exam_sources(temporary)[0]
            latest = next(
                item for item in source["all_exam_sections"]
                if item["year"] == 2026
            )
            latest_public = next(
                item for item in public_source(source, 1)["year_structures"]
                if item["year"] == 2026
            )
            self.assertEqual(latest["structure"]["question_count"], 5)
            self.assertFalse(latest_public["default_eligible"])
            self.assertIn("仅提供5题", latest["warning"])
            self.assertEqual(source["latest_year"], 2025)

    def test_hznu_prefixed_list_and_restarted_markers_get_global_slot_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "华中师范大学" / "数学分析"
            root.mkdir(parents=True)
            body = "\n".join((
                r"\section*{华中师范大学2026年数学分析真题}",
                r"\section*{140．华中师范大学2026年数学分析试卷（回忆版）}",
                r"计算题．",
                r"\begin{itemize}",
                r"  \item[（1）] 求一个明确的极限并给出完整结论与计算步骤。",
                r"  \item[（2）] 求由给定曲面围成的立体体积并说明积分区域与边界。",
                r"  \item[（3）] 计算给定曲面积分并说明曲面方向和参数条件。",
                r"\end{itemize}",
                r"\subsection{题目1}",
                r"二．解答如下问题：",
                r"\begin{itemize}",
                r"  \item[（1）] 设数列满足条件，证明相应极限结论并写出完整证明过程。",
                r"\end{itemize}",
                r"\subsection{题目1}",
                r"原题边界“（2）”：设递推数列满足条件，证明其极限结论与收敛性质。",
                r"\subsection{题目2}",
                r"三．设函数在区间上可导，证明下列两个性质并给出完整论证。",
            ))
            (root / "2026年.tex").write_text(
                "\\begin{document}\n" + body + "\n\\end{document}",
                encoding="utf-8",
            )
            source = discover_past_exam_sources(temporary)[0]
            section = source["exam_sections"][0]
            self.assertEqual(section["structure"]["question_numbers"], [1, 2, 3, 4, 5, 6])
            self.assertEqual(
                [item["source_number"] for item in extract_yearly_problems(section)],
                [1, 2, 3, 4, 5, 6],
            )
            self.assertNotIn("编号重复", section["warning"])

    def test_chinese_primary_gap_still_warns_when_nested_markers_are_ignored(self):
        structure = {
            "question_numbers": [1, 2, 4],
            "raw_question_numbers": ["一", "二", "四"],
            "source_had_markers": True,
            "ignored_marker_numbers": ["1", "2"],
            "sections": [],
        }
        self.assertTrue(
            any("主问题编号不连续" in warning for warning in _structure_warnings(structure, ""))
        )

    def test_dalian_2026_reads_complete_structure_from_formal_bank(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "大连理工大学" / "高等代数"
            root.mkdir(parents=True)
            questions = "\n".join(
                f"\\subsection{{题目{number}}} 第{number}题完整正文。"
                for number in range(1, 14)
            )
            original = "\\begin{document}\n" + questions + "\n\\end{document}\n"
            path = root / "2026年.tex"
            path.write_text(original, encoding="utf-8")

            source = discover_past_exam_sources(temporary)[0]
            latest = next(
                item for item in source["exam_sections"]
                if item["year"] == 2026
            )

            self.assertEqual(latest["source_format"], "standalone")
            self.assertEqual(latest["structure"]["question_count"], 13)
            self.assertEqual(latest["warnings"], [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_other_school_pool_is_stable_deduplicated_safe_and_recoverable(self):
        def write_exam(path, institution, subject, year, questions):
            path.parent.mkdir(parents=True, exist_ok=True)
            body = [
                r"\begin{document}",
                f"\\section*{{{institution}{year}年{subject}真题}}",
                r"\noindent\textbf{一、计算题}",
            ]
            for number, question in enumerate(questions, 1):
                body.extend((f"\\subsection{{题目{number}}}", question))
            body.append(r"\end{document}")
            path.write_text("\n".join(body), encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            duplicate = r"求函数 \(f(x)=x^2+2x\) 在实数域上的全部极值点。"
            write_exam(
                base / "大连理工大学" / "数学分析" / "2026年.tex",
                "大连理工大学", "数学分析", 2026,
                ["目标院校原题必须被严格排除，不能进入候选池。"],
            )
            write_exam(
                base / "甲大学" / "数学分析" / "2024年.tex",
                "甲大学", "数学分析", 2024,
                [duplicate, r"危险题目正文 \input{outside-file} 不得进入候选池。"],
            )
            write_exam(
                base / "乙大学" / "数学分析" / "2025年.tex",
                "乙大学", "数学分析", 2025,
                [r"求函数 \( f(x)=x^2+2x \) 在实数域上的全部极值点。",
                 r"证明闭区间上的连续函数一定能够取得最大值和最小值。",
                 r"这个题目的花括号没有闭合 \frac{1}{2 。"],
            )
            write_exam(
                base / "乙大学" / "高等代数" / "2025年.tex",
                "乙大学", "高等代数", 2025,
                ["高等代数的题目不能混入数学分析候选池。"],
            )

            snapshot_before = past_exam_source_snapshot(temporary, "数学分析")
            pool = build_other_past_candidate_pool(
                temporary, "数学分析", "大连理工大学"
            )
            self.assertEqual(len(pool), 2)
            self.assertTrue(all(item["candidate_id"].startswith("PE") for item in pool))
            self.assertTrue(all(item["source_kind"] == "past_exam" for item in pool))
            self.assertNotIn("大连理工大学", {item["source_institution"] for item in pool})
            self.assertTrue(all(item["source_file"] and not Path(item["source_file"]).is_absolute() for item in pool))
            self.assertTrue(all(item["source_section"] == "一、计算题" for item in pool))
            self.assertFalse(any("input" in item["content"] for item in pool))

            frozen_ids = [item["candidate_id"] for item in reversed(pool)]
            restored = other_past_candidates_by_ids(
                temporary, "数学分析", "大连理工大学", frozen_ids
            )
            self.assertEqual([item["candidate_id"] for item in restored], frozen_ids)

            path = base / "乙大学" / "数学分析" / "2025年.tex"
            path.write_text(path.read_text(encoding="utf-8") + "\n% snapshot change", encoding="utf-8")
            self.assertNotEqual(
                snapshot_before,
                past_exam_source_snapshot(temporary, "数学分析"),
            )
            rebuilt = build_other_past_candidate_pool(
                temporary, "数学分析", "大连理工大学"
            )
            self.assertEqual(
                {item["candidate_id"] for item in rebuilt},
                {item["candidate_id"] for item in pool},
            )

    def test_sliced_list_wrappers_are_repaired_before_candidate_assembly(self):
        def write_exam(path, institution, questions):
            path.parent.mkdir(parents=True, exist_ok=True)
            body = [r"\begin{document}", r"\noindent\textbf{一、证明题}"]
            for number, question in enumerate(questions, 1):
                body.extend((f"\\subsection{{题目{number}}}", question))
            body.append(r"\end{document}")
            path.write_text("\n".join(body), encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            write_exam(
                base / "甲大学" / "高等代数" / "2026年.tex",
                "甲大学",
                [
                    "证明下列结论：\n\\begin{itemize}\n"
                    "\\item[（1）] 第一个结论成立。",
                    "证明第二个结论成立。\n\\end{itemize}",
                    "证明：\n\\item[（1）] 第三个结论成立。\n"
                    "\\item[（2）] 第四个结论成立。",
                ],
            )
            pool = build_other_past_candidate_pool(
                temporary, "高等代数", "大连理工大学"
            )
            self.assertEqual(len(pool), 3)
            self.assertTrue(
                all(not _list_fragment_issues(item["content"]) for item in pool)
            )
            assembled = build_exam_body(
                {"sections": [{"label": "证明题", "question_count": 3}]},
                pool,
            )
            self.assertFalse(_list_fragment_issues(assembled))
            self.assertEqual(assembled.count(r"\begin{itemize}"), 2)
            self.assertEqual(assembled.count(r"\end{itemize}"), 2)

    def test_explicit_reviewed_boundaries_join_northwest_2026_continuations(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "西北大学" / "数学分析" / "2026年.tex"
            path.parent.mkdir(parents=True)
            body = [r"\begin{document}"]
            for number in (1, 3):
                body.extend((f"\\subsection{{题目{number}}}",
                             "（15 分）解答如下问题：",
                             r"\begin{itemize}",
                             r"  \item[（1）] 第一小问。"))
                if number == 1:
                    body.extend((r"  \item[（2）] 第二小问。", r"\end{itemize}",
                                 r"\subsection{题目5}",
                                 "原题边界“（3）”：第三小问。",
                                 r"\subsection{题目2}",
                                 "（15 分）一道独立题目。"))
                else:
                    body.extend((r"\end{itemize}", r"\subsection{题目3}",
                                 "原题边界“（2）”：第二小问。"))
            for number in range(4, 10):
                body.extend((f"\\subsection{{题目{number}}}",
                             "（15 分）一道独立题目。"))
            body.append(r"\end{document}")
            path.write_text("\n".join(body), encoding="utf-8")
            source = discover_past_exam_sources(temporary)[0]
            section = source["exam_sections"][0]
            self.assertEqual(section["structure"]["question_count"], 9)
            self.assertEqual(section["structure"]["question_numbers"], list(range(1, 10)))
            self.assertNotIn("主问题编号重复", section["warnings"])


if __name__ == "__main__":
    unittest.main()
