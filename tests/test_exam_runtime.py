from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import PropertyMock, patch

from cuoti_app import create_app
from cuoti_app.database import init_db
from cuoti_app.services.tex import (
    TexCompileError,
    UnsafeLatexError,
    compile_exam,
    escape_latex_text,
    validate_generated_latex,
)
from cuoti_app.services.past_exams import (
    PastExamSourceError,
    select_candidates_from_ai,
)


class TexServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @patch("cuoti_app.services.tex.shutil.which", return_value="xelatex")
    @patch("cuoti_app.services.tex.subprocess.run")
    def test_compile_uses_isolated_cwd_and_publishes_nonempty_pdf(
        self,
        run,
        _which,
    ):
        def successful_run(args, **kwargs):
            workdir = Path(kwargs["cwd"])
            (workdir / "exam_7.pdf").write_bytes(b"%PDF-1.7\n")
            return subprocess.CompletedProcess(args, 0, b"ok", b"")

        run.side_effect = successful_run
        compile_exam(
            str(self.output_dir),
            "xelatex",
            7,
            r"\begin{document}ok\end{document}",
            timeout=5,
        )

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn("-no-shell-escape", call.args[0])
            self.assertIn("-halt-on-error", call.args[0])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertNotIn("capture_output", call.kwargs)
            self.assertTrue(hasattr(call.kwargs["stdout"], "write"))
            self.assertTrue(hasattr(call.kwargs["stderr"], "write"))
            self.assertNotEqual(Path(call.kwargs["cwd"]), self.output_dir)
        self.assertGreater((self.output_dir / "exam_7.pdf").stat().st_size, 0)
        self.assertTrue((self.output_dir / "exam_7.tex").is_file())
        self.assertFalse(any(path.name.startswith(".exam_7-") for path in self.output_dir.iterdir()))

    @patch("cuoti_app.services.tex.shutil.which", return_value="xelatex")
    @patch("cuoti_app.services.tex.subprocess.run")
    def test_compile_failure_and_missing_pdf_leave_no_artifacts(self, run, _which):
        run.return_value = subprocess.CompletedProcess(
            ["xelatex"],
            1,
            b"fatal error",
            b"",
        )
        with self.assertRaises(TexCompileError):
            compile_exam(str(self.output_dir), "xelatex", 9, "bad")
        self.assertFalse((self.output_dir / "exam_9.tex").exists())
        self.assertFalse((self.output_dir / "exam_9.pdf").exists())

        run.return_value = subprocess.CompletedProcess(["xelatex"], 0, b"", b"")
        with self.assertRaisesRegex(TexCompileError, "未生成有效"):
            compile_exam(str(self.output_dir), "xelatex", 10, "no pdf")
        self.assertFalse((self.output_dir / "exam_10.tex").exists())

    @patch("cuoti_app.services.tex.shutil.which", return_value="xelatex")
    @patch("cuoti_app.services.tex.subprocess.run")
    def test_compile_error_is_actionable_and_full_output_stays_in_log(self, run, _which):
        run.return_value = subprocess.CompletedProcess(
            ["xelatex"],
            1,
            b"! Undefined control sequence.\nl.12 \\notACommand{x}\nC:\\private\\exam.tex",
            b"",
        )
        with self.assertRaises(TexCompileError) as raised:
            compile_exam(str(self.output_dir), "xelatex", 12, "bad")
        error = raised.exception
        self.assertEqual(error.code, "TEX_UNDEFINED_COMMAND")
        self.assertIn(r"\notACommand", str(error))
        self.assertIn("第 12 行", str(error))
        self.assertTrue(error.correlation_id)
        self.assertNotIn(r"C:\private", str(error))
        log = self.output_dir / ".diagnostics" / error.log_name
        self.assertTrue(log.is_file())
        self.assertIn(r"C:\private", log.read_text(encoding="utf-8"))

    @patch("cuoti_app.services.tex.shutil.which", return_value="xelatex")
    @patch(
        "cuoti_app.services.tex.subprocess.run",
        side_effect=subprocess.TimeoutExpired("xelatex", 1),
    )
    def test_compile_timeout_is_clear_and_clean(self, _run, _which):
        with self.assertRaisesRegex(TexCompileError, "编译超时"):
            compile_exam(str(self.output_dir), "xelatex", 11, "slow", timeout=1)
        self.assertFalse((self.output_dir / "exam_11.tex").exists())
        self.assertFalse((self.output_dir / "exam_11.pdf").exists())
        self.assertTrue((self.output_dir / ".diagnostics").is_dir())

    def test_latex_metadata_escaping_and_dangerous_body_rejection(self):
        self.assertEqual(
            escape_latex_text(r"A_B%{x}\y"),
            r"A\_B\%\{x\}\textbackslash{}y",
        )
        self.assertEqual(
            validate_generated_latex(r"\begin{problem}$x_1$\end{problem}"),
            r"\begin{problem}$x_1$\end{problem}",
        )
        for body in (
            r"\input{secret.txt}",
            r"\@@input secret.txt",
            r"\InputIfFileExists{secret.txt}{}{}",
            r"\CatchFileDef{\secret}{secret.txt}{}",
            r"\CatchFileBetweenTags{\secret}{secret.txt}{tag}",
            r"\begin{document}bad\end{document}",
            r"\newcommand{\x}{bad}",
            r"\write18{calc}",
            r"\href{run:program}{go}",
            r"\begin{filecontents}{x}bad\end{filecontents}",
        ):
            with self.subTest(body=body):
                with self.assertRaises(UnsafeLatexError):
                    validate_generated_latex(body)


class ExamRouteReliabilityTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "test.db"
        self.pdf_dir = self.root / "pdfs"
        self.tex_dir = self.root / "tex"
        self.past_dir = self.root / "past"
        self.pdf_dir.mkdir()
        self.tex_dir.mkdir()
        self.past_dir.mkdir()
        (self.tex_dir / "chapterB3_01_引论.tex").write_text(
            "\n".join(
                f"\\subsection{{题目{index}}}\n"
                f"INDEXED_REFERENCE_{index} content long enough for indexing."
                for index in range(1, 5)
            ),
            encoding="utf-8",
        )
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.database),
                "PDF_DIR": str(self.pdf_dir),
                "TEX_BASE": str(self.tex_dir),
                "QUESTION_BANK_DATABASE": str(
                    self.root / "question_bank.sqlite3"
                ),
                "AI_MEMORY_DIR": str(self.root / "memory"),
                "PAST_EXAM_DIR": str(self.past_dir),
                "DEEPSEEK_API_KEY": "private-test-key",
                "XELATEX_COMMAND": "xelatex-test",
            }
        )
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()
        with closing(sqlite3.connect(self.database)) as connection:
            self.subject_id, self.chapter_id = connection.execute(
                "SELECT s.id,c.id FROM subjects s "
                "JOIN chapters c ON c.subject_id=s.id ORDER BY s.id,c.id LIMIT 1"
            ).fetchone()

    def tearDown(self):
        self.temporary.cleanup()

    def _free_payload(self):
        return {
            "source": "free",
            "subject_id": self.subject_id,
            "chapters": [self.chapter_id],
            "difficulty": "中等",
            "question_count": 1,
            "types": ["计算题"],
            "note": "",
        }

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    @patch("cuoti_app.services.ai.get_settings_service")
    def test_free_exam_accepts_credential_from_windows_settings(
        self,
        settings_service,
        deepseek,
        compile_pdf,
    ):
        self.app.config["AI_API_KEY_CONFIG_OVERRIDE"] = False
        self.app.config["DEEPSEEK_API_KEY"] = ""
        settings_service.return_value.credential.return_value = (
            "windows-vault-key",
            "windows_keyring",
        )
        deepseek.return_value = r"\begin{problem}TEST\end{problem}"

        response = self.client.post("/internal/jobs/generate-exam", json=self._free_payload())

        self.assertEqual(response.status_code, 200, response.get_json())
        deepseek.assert_called_once()
        compile_pdf.assert_called_once()
        self.assertIn(
            r"\usepackage{mathtools, esint, extarrows, yhmath}",
            compile_pdf.call_args.args[3],
        )
        self.assertIn(
            r"\usepackage{array, booktabs, cases, cancel}",
            compile_pdf.call_args.args[3],
        )
        memory_files = list(
            (self.root / "memory" / "free" / "events" / "daily").glob("*.jsonl")
        )
        self.assertEqual(len(memory_files), 1)
        events = [
            json.loads(line)
            for line in memory_files[0].read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event_type"] for event in events],
            ["request", "result"],
        )

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_free_exam_references_all_selected_chapters_and_files(
        self,
        deepseek,
        compile_pdf,
    ):
        deepseek.return_value = r"\begin{problem}TEST\end{problem}"
        with closing(sqlite3.connect(self.database)) as connection:
            subject_name = connection.execute(
                "SELECT name FROM subjects WHERE id=?",
                (self.subject_id,),
            ).fetchone()[0]
            chapters = connection.execute(
                "SELECT id,name FROM chapters WHERE subject_id=? ORDER BY id LIMIT 4",
                (self.subject_id,),
            ).fetchall()
        self.assertEqual(len(chapters), 4)

        mapping = {}
        markers = []
        for index, (_chapter_id, chapter_name) in enumerate(chapters, start=1):
            filenames = []
            file_count = 2 if index == 1 else 1
            for file_index in range(1, file_count + 1):
                filename = f"coverage_{index}_{file_index}.tex"
                marker = f"COVERAGE_CHAPTER_{index}_FILE_{file_index}"
                (self.tex_dir / filename).write_text(
                    "\\subsection{\u9898\u76ee1}\n"
                    + marker
                    + " reference content long enough for parser coverage validation.",
                    encoding="utf-8",
                )
                filenames.append(filename)
                markers.append(marker)
            mapping[(subject_name, chapter_name)] = filenames

        payload = self._free_payload()
        payload["chapters"] = [chapter_id for chapter_id, _name in chapters]
        catalog_type = type(self.app.extensions["catalog_service"])
        with patch.object(
            catalog_type,
            "chapter_sources",
            new_callable=PropertyMock,
            return_value=mapping,
        ):
            response = self.client.post("/internal/jobs/generate-exam", json=payload)

        self.assertEqual(response.status_code, 200, response.get_json())
        prompt = deepseek.call_args.args[0][-1]["content"]
        for marker in markers:
            self.assertIn(marker, prompt)
        self.assertEqual(deepseek.call_args.kwargs["timeout"], 600)
        self.assertEqual(deepseek.call_args.kwargs["task"], "exam_generation")


    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_compile_failure_rolls_back_row_and_removes_files(self, deepseek, compile_pdf):
        deepseek.return_value = r"\begin{problem}测试\end{problem}"

        def fail_after_writing(pdf_dir, _command, exam_id, _latex, **_kwargs):
            (Path(pdf_dir) / f"exam_{exam_id}.pdf").write_bytes(b"partial")
            (Path(pdf_dir) / f"exam_{exam_id}.tex").write_text(
                "partial",
                encoding="utf-8",
            )
            raise TexCompileError("模拟编译失败")

        compile_pdf.side_effect = fail_after_writing
        response = self.client.post("/internal/jobs/generate-exam", json=self._free_payload())
        self.assertEqual(response.status_code, 500)
        self.assertIn("模拟编译失败", response.get_json()["error"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM exams").fetchone()[0], 0)
        self.assertEqual(list(self.pdf_dir.glob("exam_*")), [])
        self.assertEqual(list((self.root / "memory").rglob("*.jsonl")), [])

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_dangerous_ai_latex_is_not_saved_or_compiled(self, deepseek, compile_pdf):
        deepseek.return_value = r"\input{private.txt}"
        response = self.client.post("/internal/jobs/generate-exam", json=self._free_payload())
        self.assertEqual(response.status_code, 500)
        self.assertIn("禁用命令", response.get_json()["error"])
        compile_pdf.assert_not_called()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM exams").fetchone()[0], 0)
        self.assertEqual(list((self.root / "memory").rglob("*.jsonl")), [])

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_wrong_question_count_is_corrected_once(
        self,
        deepseek,
        compile_pdf,
    ):
        deepseek.side_effect = [
            (
                r"\begin{problem}第一题\end{problem}"
                r"\begin{problem}多余题\end{problem}"
            ),
            r"\begin{problem}纠正后的唯一题目\end{problem}",
        ]
        payload = self._free_payload()
        payload["target"] = "考研训练"
        response = self.client.post("/internal/jobs/generate-exam", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(deepseek.call_count, 2)
        correction = deepseek.call_args_list[1].args[0][-1]["content"]
        self.assertIn("严格满足题量、难度配额和JSON格式", correction)
        self.assertIn("纠正后的唯一题目", response.get_json()["content"])
        compile_pdf.assert_called_once()
        with closing(sqlite3.connect(self.database)) as connection:
            config = json.loads(
                connection.execute(
                    "SELECT config FROM exams ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
        self.assertEqual(config["difficulty"], "中等")
        self.assertEqual(config["target"], "考研训练")
        first_prompt = deepseek.call_args_list[0].args[0][1]["content"]
        self.assertIn("难度等级：中等", first_prompt)
        self.assertIn("使用场景：考研训练", first_prompt)

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_repeated_wrong_question_count_is_not_saved(
        self,
        deepseek,
        compile_pdf,
    ):
        deepseek.return_value = (
            r"\begin{problem}第一题\end{problem}"
            r"\begin{problem}多余题\end{problem}"
        )
        response = self.client.post(
            "/internal/jobs/generate-exam",
            json=self._free_payload(),
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("连续两次", response.get_json()["error"])
        self.assertEqual(deepseek.call_count, 2)
        compile_pdf.assert_not_called()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM exams").fetchone()[0],
                0,
            )

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_legacy_postgraduate_difficulty_maps_to_target(
        self,
        deepseek,
        compile_pdf,
    ):
        deepseek.return_value = r"\begin{problem}测试\end{problem}"
        payload = self._free_payload()
        payload["difficulty"] = "考研"
        response = self.client.post("/internal/jobs/generate-exam", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        prompt = deepseek.call_args.args[0][-1]["content"]
        self.assertIn("难度等级：困难", prompt)
        self.assertIn("使用场景：考研训练", prompt)
        with closing(sqlite3.connect(self.database)) as connection:
            config = json.loads(
                connection.execute(
                    "SELECT config FROM exams ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
        self.assertEqual(config["difficulty"], "困难")
        self.assertEqual(config["target"], "考研训练")

    def test_mistake_exam_reference_limit_is_twenty(self):
        response = self.client.post(
            "/internal/jobs/generate-exam",
            json={
                "source": "mistakes",
                "mistake_ids": list(range(1, 22)),
                "difficulty": "中等",
                "target": "日常练习",
                "question_count": 5,
                "types": ["计算题"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("最多选择 20 道错题", response.get_json()["error"])

    def test_adapted_past_question_requires_latex_math_markup(self):
        candidates = [
            {
                "candidate_id": "QB001",
                "content": r"求 \( \oiint_S z^2\,\mathrm{d}S \)",
                "chapter": "曲面积分",
                "source_file": "chapter.tex",
                "source_number": 1,
            }
        ]
        invalid = json.dumps(
            {
                "selected": [
                    {
                        "candidate_id": "QB001",
                        "position": 1,
                        "adapted_content": "利用 Gauss 公式计算 ∮_S z^2 dS",
                    }
                ]
            },
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(PastExamSourceError, "Unicode 数学符号"):
            select_candidates_from_ai(
                invalid,
                candidates,
                1,
                allow_adaptation=True,
            )

        valid = json.dumps(
            {
                "selected": [
                    {
                        "candidate_id": "QB001",
                        "position": 1,
                        "adapted_content": (
                            r"利用 Gauss 公式计算 "
                            r"\( \oiint_S z^2\,\mathrm{d}S \)"
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        )
        selection = select_candidates_from_ai(
            valid,
            candidates,
            1,
            allow_adaptation=True,
        )
        self.assertTrue(selection[0]["adapted"])

    def test_invalid_json_and_cross_subject_chapter_are_rejected(self):
        response = self.client.post(
            "/internal/jobs/generate-exam",
            data="not-json",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 415)

        with closing(sqlite3.connect(self.database)) as connection:
            other_subject = connection.execute(
                "SELECT id FROM subjects WHERE id<>? ORDER BY id LIMIT 1",
                (self.subject_id,),
            ).fetchone()[0]
            other_chapter = connection.execute(
                "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
                (other_subject,),
            ).fetchone()[0]
        payload = self._free_payload()
        payload["chapters"] = [other_chapter]
        response = self.client.post("/internal/jobs/generate-exam", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不属于", response.get_json()["error"])

    def test_delete_preserves_files_and_pdf_route_requires_active_record(self):
        with closing(sqlite3.connect(self.database)) as connection:
            cursor = connection.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (self.subject_id, "测试", "{}", "body", "2026-07-26 12:00"),
            )
            exam_id = cursor.lastrowid
            connection.commit()
        pdf_path = self.pdf_dir / f"exam_{exam_id}.pdf"
        tex_path = self.pdf_dir / f"exam_{exam_id}.tex"
        pdf_path.write_bytes(b"%PDF-test")
        tex_path.write_text("test", encoding="utf-8")

        served = self.client.get(f"/pdfs/exam_{exam_id}.pdf")
        self.assertEqual(served.status_code, 200)
        served.close()
        self.assertEqual(self.client.get("/pdfs/other.pdf").status_code, 404)
        self.assertEqual(self.client.get("/pdfs/exam_99999.pdf").status_code, 404)

        response = self.client.delete(f"/api/v2/exams/{exam_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pdf_path.read_bytes(), b"%PDF-test")
        self.assertEqual(tex_path.read_text(encoding="utf-8"), "test")
        self.assertEqual(
            self.client.get(f"/pdfs/exam_{exam_id}.pdf").status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/api/v2/exams/{exam_id}").status_code,
            404,
        )
        restored = self.client.post(f"/api/v2/trash/exams/{exam_id}/restore")
        self.assertEqual(restored.status_code, 200)
        served = self.client.get(f"/pdfs/exam_{exam_id}.pdf")
        self.assertEqual(served.status_code, 200)
        served.close()


if __name__ == "__main__":
    unittest.main()
