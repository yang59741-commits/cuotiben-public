import re
import unittest
from pathlib import Path
from PIL import Image, ImageChops
from tests.frontend_sources import application_styles, script_source, template_source


ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = ROOT / "cuoti_app" / "static" / "js"
STATIC_CSS = ROOT / "cuoti_app" / "static" / "css" / "app.css"
LOGO = ROOT / "cuoti_app" / "static" / "img" / "logo.png"
PREVIEW_BEAR = ROOT / "cuoti_app" / "static" / "img" / "preview-bear.png"
TEMPLATE = ROOT / "cuoti_app" / "templates" / "index.html"


class FrontendSafetySourceTests(unittest.TestCase):
    def read_js(self, name):
        return script_source(name)

    def test_latex_toolbar_uses_external_exact_insertions(self):
        template = template_source()
        library = self.read_js("library.js")
        self.assertNotIn('onclick="insertLatex(', template)
        for command in (
            "bold",
            "italic",
            "display-math",
            "inline-math",
            "proof",
            "enumerate",
            "aligned",
        ):
            self.assertIn(f'data-latex-command="{command}"', template)
        for insertion in (
            r"bold:['\\textbf{','}']",
            r"italic:['\\textit{','}']",
            r"'display-math':['$$\n','\n$$']",
            r"proof:['\\begin{proof}\n','\n\\end{proof}']",
            r"enumerate:['\\begin{enumerate}\n  \\item ','\n\\end{enumerate}']",
            r"aligned:['\\begin{aligned}\n','\n\\end{aligned}']",
        ):
            self.assertIn(insertion, library)

    def test_legacy_doc_is_not_offered(self):
        template = template_source()
        self.assertIn('accept=".pdf,.docx,.tex,.txt"', template)
        self.assertIn("不会上传原始 PDF，也不会调用 Mathpix", template)
        self.assertNotRegex(template, re.compile(r'accept="[^"]*\.doc(?:,|")'))

    def test_api_rejects_http_and_non_json_failures(self):
        core = self.read_js("core.js")
        self.assertIn("if(!contentType.includes('application/json'))", core)
        self.assertIn("if(!response.ok)", core)
        self.assertIn("requestError.status=response.status", core)

    def test_dynamic_renderers_escape_stored_text(self):
        mistakes = self.read_js("mistakes.js")
        stats = self.read_js("stats.js")
        exams = self.read_js("exam.js")
        self.assertIn("${escapeHTML(item.chapter||'未归类')}", mistakes)
        self.assertIn("${escapeHTML(item.note||'')}", mistakes)
        self.assertIn("${escapeHTML(item.chapter||'未归类')}", stats)
        self.assertIn("escapeHTML(value.tip||'')", self.read_js("daily.js"))
        self.assertIn("${escapeHTML(exam.title||'')}", exams)
        self.assertIn("safeLocalURL(exam.pdf_url)", exams)

    def test_note_editor_uses_v2_revision_contract_and_inline_conflicts(self):
        mistakes = self.read_js("mistakes.js")
        self.assertIn("method:'PATCH'", mistakes)
        self.assertIn("apiAdapter.updateNote(", mistakes)
        self.assertIn("safeInteger(item.revision)", mistakes)
        self.assertIn("JSON.stringify({revision,note})", mistakes)
        self.assertIn("escapeHTML(item.note||'')", mistakes)
        self.assertIn("js-note-load-latest", mistakes)
        self.assertIn("js-note-keep-draft", mistakes)
        self.assertNotIn("innerHTML=input.value", mistakes)

    def test_stats_hides_internal_values_and_uses_inline_read_errors(self):
        stats = self.read_js("stats.js")
        current = stats[stats.index("async function loadStats("):]
        self.assertNotIn("JSON.stringify(item.range", current)
        self.assertNotIn("showModal('统计读取失败'", current)
        self.assertIn("pass:'本次答对'", stats)
        self.assertIn("again:'需要再练'", stats)
        self.assertIn("day:'按日'", stats)
        self.assertIn("formatInsightRange(item.range)", current)
        self.assertIn("problemPreviewHTML(item.message,'insight-message')", current)
        self.assertIn("stats-load-error", current)

    def test_stats_ledger_uses_four_two_two_responsive_grid(self):
        css = application_styles()
        self.assertIn("#stat-box.stats-row{display:grid;grid-template-columns:repeat(4", css)
        self.assertIn("@media(max-width:1100px){#stat-box.stats-row{grid-template-columns:repeat(2", css)
        mobile = css[css.index("@media(max-width:680px){\n  .stats-scope"):]
        self.assertIn("#stat-box.stats-row{grid-template-columns:repeat(2", mobile)

    def test_import_exposes_merge_and_confirmed_replace(self):
        source = self.read_js("data.js")
        self.assertIn("previewImport('merge')", source)
        self.assertIn("previewImport('replace')", source)
        self.assertIn("id=\"import-replace-confirm\"", source)
        self.assertIn("new URLSearchParams({mode})", source)
        self.assertIn("/api/v3/import/preview", source)
        self.assertIn("/api/v3/import/stage", source)
        self.assertIn("/api/v3/import/stage", source)
        self.assertIn("runJobRequest('data-import'", source)
        self.assertIn("X-Import-Preview-Token", source)
        self.assertIn("confirmation.value.trim()", source)
        self.assertIn("resolutions,", source)
        self.assertIn("默认保留本地；勾选后采用导入值", source)

    def test_brand_logo_and_favicon_are_present(self):
        template = template_source()
        self.assertTrue(LOGO.is_file())
        self.assertIn("filename='img/logo.png'", template)
        self.assertIn('alt="错题本 logo"', template)
        self.assertIn('rel="icon"', template)

    def test_live_previews_show_the_waking_bear_sprite(self):
        summaries = re.findall(
            r'<details class="math-live-preview"[^>]*><summary>(.*?)</summary>',
            template_source(),
        )
        css = application_styles()
        worker = (STATIC_JS.parent / "sw.js").read_text(encoding="utf-8")
        self.assertEqual(len(summaries), 4)
        for summary in summaries:
            self.assertTrue(summary.startswith(
                '<span class="math-live-preview-bear" aria-hidden="true"></span>'
            ))
            self.assertNotIn('data-icon="eye"', summary)
        self.assertIn(
            ".math-live-preview-bear{flex:none;width:20px;height:20px;"
            "background:url('../img/preview-bear.png') 0 0/700% 100% no-repeat}",
            css,
        )
        self.assertIn(".math-live-preview[open] .math-live-preview-bear{background-position:100% 0}", css)
        self.assertIn('[data-motion="wake"]{animation:previewBearWake 420ms steps(6,jump-start)', css)
        self.assertIn('[data-motion="doze"]{animation:previewBearDoze 280ms steps(6,jump-start)', css)
        self.assertIn(
            "@media(prefers-reduced-motion:reduce){.math-live-preview-bear[data-motion]{animation:none}}",
            css,
        )
        self.assertIn("'/static/img/preview-bear.png'", worker)
        with Image.open(PREVIEW_BEAR) as sprite:
            self.assertEqual((sprite.mode, sprite.size), ("RGBA", (7 * 60, 60)))
            frames = [sprite.crop((index * 60, 0, index * 60 + 60, 60)) for index in range(7)]
        colours = [frame.convert("RGB") for frame in frames]
        for frame, colour in zip(frames, colours):
            alpha = frame.getchannel("A")
            # Only the rounded tile is opaque; the background around it is cut away.
            for corner in ((0, 0), (59, 0), (0, 59), (59, 59)):
                self.assertEqual(alpha.getpixel(corner), 0)
            self.assertEqual(alpha.getpixel((30, 30)), 255)
            self.assertIsNone(ImageChops.difference(alpha, frames[0].getchannel("A")).getbbox())
            # Only the eyes move between frames, so the rest of the bear stays still.
            changed = ImageChops.difference(colour, colours[0]).getbbox()
            if changed is not None:
                self.assertGreaterEqual(changed[1], 15)
                self.assertLessEqual(changed[3], 45)
        self.assertIsNotNone(ImageChops.difference(colours[0], colours[6]).getbbox())

    def test_warm_brand_tokens_replace_legacy_blue_brand(self):
        css = application_styles().lower()
        for token in (
            "--bg:#f9f4ef",
            "--card:#fffdf9",
            "--accent:#6b4e34",
            "--accent-soft:#ede4db",
            "--text:#49372a",
        ):
            self.assertIn(token, css)
        for legacy in ("#4a8eb5", "#6badd0", "#3d7ea6", "#5a9cc4"):
            self.assertNotIn(legacy, css)

    def test_primary_palette_meets_text_contrast_target(self):
        def luminance(color):
            channels = [
                int(color[index : index + 2], 16) / 255
                for index in (1, 3, 5)
            ]
            linear = [
                value / 12.92
                if value <= 0.04045
                else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return (
                0.2126 * linear[0]
                + 0.7152 * linear[1]
                + 0.0722 * linear[2]
            )

        def contrast(first, second):
            high, low = sorted(
                (luminance(first), luminance(second)),
                reverse=True,
            )
            return (high + 0.05) / (low + 0.05)

        for foreground, background in (
            ("#49372a", "#f9f4ef"),
            ("#756355", "#fffdf9"),
            ("#ffffff", "#6b4e34"),
            ("#ffffff", "#52697a"),
            ("#ffffff", "#8c5737"),
        ):
            self.assertGreaterEqual(contrast(foreground, background), 4.5)

    def test_mistake_search_is_debounced_abortable_and_cursor_paginated(self):
        source = self.read_js("mistakes.js")
        template = template_source()
        self.assertIn("window.setTimeout(()=>loadMistakes(),300)", source)
        self.assertIn("new AbortController()", source)
        self.assertIn("revision!==state.requestRevision", source)
        self.assertIn("payload.next_cursor", source)
        self.assertIn("payload.has_more", source)
        self.assertIn("loadMistakes({append:true})", source)
        self.assertNotIn('oninput="loadMistakes()"', template)
        self.assertIn('id="mistake-load-more"', template)

    def test_mistake_append_deduplicates_and_never_requests_without_cursor(self):
        source = self.read_js("mistakes.js")
        self.assertIn("function mergeUniqueMistakes(current,incoming", source)
        self.assertIn("seenPublicIds.has(publicId)", source)
        self.assertIn("if(append&&(!state.pager.hasMore||!state.pager.nextCursor))return", source)
        self.assertIn("if(!append)state.pager.reset()", source)
        self.assertIn("renderMistakes(append?merged.added:state.items", source)
        self.assertIn("incomingDates=[...template.content.children]", source)
        self.assertNotIn(
            "template.content.querySelectorAll(':scope > .date-group')", source
        )

    def test_daily_review_uses_its_own_results_and_exam_recommendations_api(self):
        loader = self.read_js("feature-loader.js")
        controller = self.read_js("daily-review/controller.js")
        api_source = self.read_js("daily-review/api.js")
        exam = self.read_js("exam.js")
        worker = (STATIC_JS.parent / "sw.js").read_text(encoding="utf-8")
        template = (
            TEMPLATE.parent / "partials" / "pages" / "exam.html"
        ).read_text(encoding="utf-8")
        review_template = (
            TEMPLATE.parent / "partials" / "pages" / "review.html"
        ).read_text(encoding="utf-8")
        self.assertIn("/static/js/review/controller.js", loader)
        self.assertIn("/static/js/daily-review/controller.js", loader)
        self.assertIn("/static/js/daily-review/controller.js", worker)
        self.assertIn("runJobRequest('mistake-export'", controller)
        self.assertIn("export_context:'daily_review'", controller)
        self.assertIn("'/api/v2/daily-review/'+(sessionId?'sessions/'+encodeURIComponent(sessionId):'current')", api_source)
        self.assertIn("/items/${encodeURIComponent(itemId)}/result", api_source)
        self.assertNotIn("/api/v2/mistakes/", api_source)
        self.assertIn("/api/v2/daily-review/recommendations?", exam)
        self.assertIn("selectedExamMistakeIds.size>=MAX_EXAM_MISTAKE_REFERENCES", exam)
        self.assertIn('id="exam-recommendation-status"', template)
        self.assertNotIn('id="exam-daily-recommendations"', template)
        self.assertIn("每日自主复习", review_template)

    def test_mistake_pdf_export_selection_and_history_contract(self):
        source = self.read_js("mistakes.js")
        css = application_styles()
        template = template_source()
        self.assertIn("selectedPublicIds:new Set()", source)
        self.assertIn("selectedPublicIds.has(String(item.public_id||''))", source)
        self.assertIn(
            "if(item.public_id)state.selectedPublicIds.add(String(item.public_id))",
            source,
        )
        self.assertIn("runJobRequest('mistake-export'", source)
        self.assertIn("/api/v2/mistake-exports?", source)
        self.assertIn("safeLocalURL(item.download_url)", source)
        self.assertIn("summary.textContent=", source)
        self.assertIn(
            "if(append&&(!state.exportPager.hasMore||!state.exportPager.nextCursor))return",
            source,
        )
        self.assertIn("if(!id||seenIds.has(id))return false", source)
        self.assertIn(".mistake-export-history-more[hidden]{display:none}", css)
        self.assertIn('class="js-select-mistake"', source)
        self.assertIn('id="mistake-export-bar"', template)
        self.assertIn('id="mistake-export-pdf" disabled', template)
        self.assertIn('id="mistake-export-history"', template)

    def test_mistake_checkbox_overrides_global_input_sizing(self):
        css = application_styles()
        match = re.search(r"\.mistake-select-control input\{([^}]*)\}", css)
        self.assertIsNotNone(match)
        declarations = match.group(1)
        for constraint in (
            "flex:0 0 16px",
            "min-width:16px",
            "width:16px",
            "height:16px",
            "padding:0",
        ):
            self.assertIn(constraint, declarations)

    def test_native_controls_and_dialog_semantics_are_present(self):
        template = template_source()
        core = self.read_js("core.js")
        self.assertIn('class="nav-btn ac"', template)
        self.assertIn('role="tablist"', template)
        self.assertIn('role="tab"', template)
        self.assertIn('role="dialog" aria-modal="true"', template)
        self.assertIn('label for="s-subject"', template)
        self.assertIn('button type="button" class="drop-zone"', template)
        self.assertIn("trapModalFocus", core)
        self.assertIn("modalReturnFocus", core)

    def test_review_has_mobile_cards_and_intermediate_scroll(self):
        review = self.read_js("review/render.js")
        css = (STATIC_CSS.parent / "pages" / "review.css").read_text(
            encoding="utf-8"
        )
        self.assertIn('class="review-table-scroll"', review)
        self.assertIn('class="review-card"', review)
        self.assertIn(".review-table-scroll{width:100%;overflow-x:auto}", css)
        self.assertIn(".review-table-scroll{display:none}", css)
        self.assertIn(".review-card-list{display:block}", css)

    def test_mathjax_is_local_and_has_failure_status(self):
        template = template_source()
        core = self.read_js("core.js")
        self.assertIn("filename='vendor/mathjax/es5/tex-chtml.js'", template)
        self.assertNotIn("cdn.jsdelivr.net", template)
        self.assertIn('id="math-status" role="status"', template)
        self.assertIn("showMathUnavailable", core)

    def test_v2_settings_are_masked_in_the_ui_contract(self):
        template = template_source()
        settings = self.read_js("settings.js")
        self.assertIn('id="p-settings"', template)
        self.assertIn("/api/v2/settings/ai", settings)
        self.assertIn("/api/v2/settings/ai/test", settings)
        self.assertIn("/api/v2/settings/ai/migrate-legacy", settings)
        self.assertIn("api_key_configured", settings)
        self.assertIn("payload?.has_api_key", settings)
        self.assertIn("payload.api_key_action=key?'replace':'keep'", settings)
        self.assertIn("if(key)payload.api_key=key", settings)
        self.assertNotIn("payload.api_key||", settings)
        self.assertIn('id="ai-route-daily"', template)
        self.assertNotIn('id="ai-route-daily" disabled', template)
        self.assertIn("daily_analysis:'ai-route-daily'", settings)
        self.assertIn("aiSettingsDirty", settings)
        self.assertIn("rebaseAISettings", settings)
        self.assertIn("resolveAISettingsConflict", settings)
        self.assertIn("/api/v2/settings/ai/reset", settings)
        self.assertIn("/api/v2/settings/ai/credential", settings)
        self.assertNotIn("localStorage", settings)
        self.assertNotIn("sessionStorage", settings)

    def test_global_jobs_panel_uses_v2_lifecycle_endpoints(self):
        template = template_source()
        jobs = self.read_js("jobs.js")
        self.assertIn('id="task-panel"', template)
        self.assertIn("/api/v2/jobs?state=active", jobs)
        self.assertIn("+'/cancel'", jobs)
        self.assertIn("+'/retry'", jobs)
        self.assertIn("+'/result'", jobs)
        self.assertIn("payload?.progress?.current", jobs)
        for kind in (
            "batch-mistakes",
            "classification",
            "ocr",
            "exam-generation",
            "exam-recompile",
            "analysis",
            "data-import",
            "mistake-export",
        ):
            combined = "\n".join(
                self.read_js(name)
                for name in (
                    "mistakes.js", "library.js", "exam.js", "stats.js", "data.js", "daily-analysis/api.js"
                )
            )
            self.assertIn(f"runJobRequest('{kind}'", combined)
        self.assertIn("localStorage.setItem(JOB_TRACKING_KEY", jobs)
        self.assertIn("sessionStorage.setItem(JOB_SUBMISSIONS_KEY", jobs)
        self.assertIn("headers:{'Idempotency-Key':submission.key}", jobs)
        self.assertIn("const missing=[...trackedJobIds]", jobs)
        self.assertIn("jobInitialHistoryLoaded?'/api/v2/jobs?state=active':'/api/v2/jobs?limit=50'", jobs)
        self.assertIn("if(document.hidden){scheduleJobPolling();return}", jobs)
        self.assertIn("document.hidden?30000:(hasActive?1200:10000)", jobs)
        self.assertIn("visibilitychange", jobs)
        self.assertIn("window.addEventListener('online',refreshJobPolling)", jobs)

    def test_v2_due_reviews_cursor_resets_and_rejects_stale_responses(self):
        template = template_source()
        state = self.read_js("review/state.js")
        api_source = self.read_js("review/api.js")
        controller = self.read_js("review/controller.js")
        review_template = (
            TEMPLATE.parent / "partials" / "pages" / "review.html"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/v2/reviews/due?", api_source)
        self.assertIn("createCursorPager(30)", state)
        self.assertIn("state.pager.params(append)", controller)
        self.assertIn("function mergeUniqueReviewItems(current,incoming)", controller)
        self.assertIn("if(!append)state.pager.reset()", controller)
        self.assertIn("new AbortController()", controller)
        self.assertIn("revision!==state.requestRevision", controller)
        self.assertIn('id="spaced-review-load-more"', review_template)
        self.assertIn("loadPage({append:true})", controller)

    def test_page_actions_are_delegated_and_shared_pager_is_available(self):
        template = template_source()
        core = self.read_js("core.js")
        self.assertNotIn("onclick=", template)
        self.assertIn("const UI_ACTIONS=Object.freeze", core)
        self.assertIn("event.target.closest('[data-action]')", core)
        self.assertIn("function createCursorPager(limit=30)", core)
        self.assertIn("pager:createCursorPager", core)

    def test_difficulty_stars_and_attempt_timeline_are_accessible(self):
        template = template_source()
        exams = self.read_js("exam.js")
        css = application_styles()
        self.assertIn('id="e-diff-stars" aria-live="polite"', template)
        self.assertIn('id="em-diff-stars" aria-live="polite"', template)
        self.assertIn("难度 ${level} 星，共 3 星", exams)
        self.assertIn("exam-attempt-marker", exams)
        self.assertIn(".exam-attempt-marker", css)

    def test_v2_exam_candidates_cursor_resets_and_rejects_stale_responses(self):
        template = template_source()
        exams = self.read_js("exam.js")
        self.assertIn("/api/v2/exam-candidates?", exams)
        self.assertIn("params=new URLSearchParams({limit:'30',subject_id:subjectId})", exams)
        self.assertIn("params.set('after',examMistakeNextCursor)", exams)
        self.assertIn("mergeExamMistakes(data,{replace:!append})", exams)
        self.assertIn(
            "if(!append){examMistakeNextCursor=null;examMistakeHasMore=false}",
            exams,
        )
        self.assertIn("examMistakeRequestController=new AbortController()", exams)
        self.assertIn("revision!==examMistakeRequestRevision", exams)
        self.assertIn("page?.has_more", exams)
        self.assertIn('id="exam-candidates-more"', exams)
        self.assertIn("loadExamMistakes({append:true})", exams)
        self.assertIn("selectedExamMistakeIds:new Set()", exams)
        self.assertIn("selectedExamMistakeIds.has(safeInteger(m.id))", exams)
        self.assertIn('id="em-subject"', template)
        self.assertIn('id="em-chapter"', template)
        self.assertIn("最多 20 道", template)

    def test_exam_difficulty_and_target_are_separate(self):
        template = template_source()
        exams = self.read_js("exam.js")
        for control in ("e-diff", "e-target", "em-diff", "em-training-target"):
            self.assertIn(f'id="{control}"', template)
        self.assertNotIn("<option>考研</option>", template)
        self.assertIn('value="考研训练"', template)
        self.assertIn("target:document.getElementById('e-target').value", exams)
        self.assertIn("target:'日常练习'", exams)
        self.assertNotIn('id="em-target"', template)

    def test_exam_attempt_ui_records_no_answers(self):
        exams = self.read_js("exam.js")
        self.assertIn("/api/v2/exams/${examId}/attempts", exams)
        self.assertIn("/api/v2/exam-attempts/${attempt.public_id}", exams)
        self.assertIn("/return-mistake", exams)
        self.assertIn("method:'DELETE'", exams)
        self.assertIn("void renderMath(body)", exams)
        self.assertIn("latexToHtml(String(item.display_content||item.content||''))", exams)
        self.assertIn("不存储任何答案", exams)
        self.assertNotIn("attempt-answer", exams)

    def test_past_exam_controls_use_deterministic_year_and_candidate_source(self):
        template = template_source()
        exams = self.read_js("exam.js")
        css = application_styles()

        for control in (
            "ep-institution",
            "ep-subject",
            "ep-reference-year",
            "ep-candidate-source",
            "ep-adaptation",
        ):
            self.assertIn(f'id="{control}"', template)
        self.assertRegex(
            template,
            r'<option value="ordinary" selected>普通题库</option>',
        )
        adaptation = re.search(
            r'<input type="checkbox" id="ep-adaptation"(?P<attrs>[^>]*)>',
            template,
        )
        self.assertIsNotNone(adaptation)
        self.assertNotIn("checked", adaptation.group("attrs"))

        self.assertNotIn("['大连理工大学',0]", exams)
        self.assertNotIn("['湖南师范大学',1]", exams)
        self.assertIn("Number(secondUsable)-Number(firstUsable)", exams)
        self.assertIn("source?.year_structures", exams)
        self.assertIn("raw?.available===true&&raw?.usable===true", exams)
        self.assertIn("raw?.profile_selectable===true", exams)
        self.assertIn("String(item.warning??'').trim()", exams)
        self.assertIn("${escapeHTML(warnings.join('；'))}", exams)
        self.assertIn("reference_year:referenceYear", exams)
        self.assertIn("candidate_source:candidateSource", exams)
        self.assertIn("candidateSource==='mixed'", exams)
        self.assertIn("safeInteger(structure.question_count)<2", exams)
        self.assertNotIn("DeepSeek V4 Pro", template + exams)
        self.assertNotIn("随机学习", template + exams)
        self.assertIn(".past-source-warning", css)
        self.assertIn(".past-candidate-note.is-mixed", css)

    def test_free_exam_uses_effective_question_bank_coverage(self):
        template = template_source()
        exams = self.read_js("exam.js")
        css = application_styles()
        self.assertIn('id="exam-bank-coverage"', template)
        self.assertIn('role="status" aria-live="polite"', template)
        self.assertIn("/api/v2/question-bank/coverage?", exams)
        self.assertIn("chapter_ids:chapterIds.join(',')", exams)
        self.assertIn("payload?.total_questions", exams)
        self.assertIn("payload?.duplicate_count", exams)
        self.assertIn("payload?.excluded_count", exams)
        self.assertIn("payload?.snapshot_id", exams)
        self.assertIn("payload?.errors", exams)
        self.assertIn("examBankCoverageRequestController=new AbortController()", exams)
        self.assertIn("revision!==examBankCoverageRequestRevision", exams)
        self.assertIn("coveredChapterIds", exams)
        self.assertIn("safeInteger(chapter?.total_questions)>0", exams)
        self.assertIn(".exam-bank-coverage.is-ready", css)
        self.assertIn(".exam-bank-coverage.is-error", css)

    def test_free_exam_hides_field_theory_intro_chapter(self):
        exams = self.read_js("exam.js")
        self.assertIn("function isExcludedExamChapter(chapter)", exams)
        self.assertIn("name.includes('场论初步')", exams)
        self.assertIn(
            "!isExcludedExamChapter(chapter)&&",
            exams,
        )

    def test_classic_scripts_do_not_redeclare_global_lexical_names(self):
        template = template_source()
        script_names = re.findall(
            r"filename='js/([^']+\.js)'",
            template,
        )
        self.assertTrue(script_names)
        lexical = {}
        var_like = {}
        for script_name in script_names:
            source = self.read_js(script_name)
            for match in re.finditer(
                r"^(?:const|let)\s+([A-Za-z_$][\w$]*)",
                source,
                re.MULTILINE,
            ):
                name = match.group(1)
                self.assertNotIn(
                    name,
                    lexical,
                    f"{name} is declared in both {lexical.get(name)} and {script_name}",
                )
                self.assertNotIn(
                    name,
                    var_like,
                    f"{name} conflicts with {var_like.get(name)} in {script_name}",
                )
                lexical[name] = script_name
            for match in re.finditer(
                r"^(?:(?:async\s+)?function|var)\s+([A-Za-z_$][\w$]*)",
                source,
                re.MULTILINE,
            ):
                name = match.group(1)
                self.assertNotIn(
                    name,
                    lexical,
                    f"{name} conflicts with {lexical.get(name)} in {script_name}",
                )
                var_like.setdefault(name, script_name)

    def test_v2_exam_history_cursor_resets_and_rejects_stale_responses(self):
        template = template_source()
        exams = self.read_js("exam.js")
        self.assertIn("/api/v2/exams?", exams)
        self.assertIn("params.set('after',examHistoryNextCursor)", exams)
        self.assertIn(
            "examHistoryItems=append?[...examHistoryItems,...items]:items",
            exams,
        )
        self.assertIn(
            "if(!append){examHistoryNextCursor=null;examHistoryHasMore=false}",
            exams,
        )
        self.assertIn("examHistoryRequestController=new AbortController()", exams)
        self.assertIn("revision!==examHistoryRequestRevision", exams)
        self.assertIn("examHistoryHasMore=Boolean(page?.has_more", exams)
        self.assertIn('id="exam-history-load-more"', template)
        self.assertIn("loadExamHistory({append:true})", exams)

    def test_visual_refinement_uses_warm_tokens_and_no_legacy_cold_grays(self):
        css = application_styles().lower()
        for token in (
            "--gray-100:#f7f4f0",
            "--gray-300:#d9cfc3",
            "--gray-600:#6b6258",
            "--placeholder:#a39888",
            "--fs-xs:10px",
            "--radius-control:10px",
            "--radius-container:14px",
            "--dur-fast:150ms",
            "--chart-pass:#2e7747",
        ):
            self.assertIn(token, css)
        for cold_gray in (
            "#cbd5e1",
            "#d1d5db",
            "#e2e8f0",
            "#9ca3af",
            "#64748b",
            "#6b7280",
            "#475569",
            "#334e5a",
        ):
            self.assertNotIn(cold_gray, css)
        self.assertIn("border-color:var(--accent-light)", css)
        self.assertIn("input::placeholder,textarea::placeholder", css)
        self.assertIn(".btn:hover{transform:translatey(-1px)}", css)

    def test_toast_modal_and_empty_state_share_semantic_components(self):
        core = self.read_js("core.js")
        template = template_source()
        mistakes = self.read_js("mistakes.js")
        review = self.read_js("review.js")
        for title in ("提示", "已完成", "请注意", "操作失败"):
            self.assertIn(f"title:'{title}'", core)
        self.assertIn("toast-icon", core)
        self.assertIn("toast-title", core)
        self.assertIn("function emptyStateHTML(title,options={})", core)
        self.assertIn("return window.Cuoti.ui.render.emptyState(title,options)", core)
        self.assertIn("data-action=\"navigate\"", core)
        self.assertIn("actionLabel:'去标记错题',page:'add'", mistakes)
        self.assertIn("actionLabel:'查看错题列表',page:'list'", review)
        self.assertIn('id="modal-type-icon"', template)
        self.assertIn("document.getElementById('modal-type-icon').innerHTML", core)
        self.assertIn("modal.querySelector('.modal-footer button')", core)
        self.assertNotIn(".modal-footer button,.modal-close,.modal-box", core)

    def test_heatmap_is_discrete_labeled_and_quotes_are_not_italic(self):
        stats = self.read_js("stats.js")
        css = application_styles().lower()
        self.assertIn("function heatmapLevel(value,maximum)", stats)
        self.assertIn('data-level="${F.heatmapLevel(row?.reviews,max)}"', stats)
        self.assertIn('aria-label="学习强度从少到多"', stats)
        for level in range(5):
            self.assertIn(f'--heat-{level}:', css)
        self.assertIn("daily-quote", self.read_js("daily.js"))
        self.assertNotIn("font-style:italic", self.read_js("daily.js"))
        self.assertIn(".daily .daily-quote", css)
        self.assertIn("font-style:normal", css)

    def test_visual_motion_has_touch_and_reduced_motion_fallbacks(self):
        css = application_styles()
        self.assertIn("[hidden]{display:none!important}", css)
        self.assertIn("@media(max-width:1180px) and (pointer:coarse)", css)
        self.assertIn(".drop-zone:active", css)
        self.assertIn(".fld-row>.fld-inline{min-width:0;flex:1 1 100%!important}", css)
        self.assertIn("@media(prefers-reduced-motion:reduce)", css)
        self.assertIn("transition:none!important", css)


if __name__ == "__main__":
    unittest.main()
