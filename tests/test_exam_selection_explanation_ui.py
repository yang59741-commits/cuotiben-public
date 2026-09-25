from tests.frontend_sources import script_source
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cuoti_app" / "static" / "js" / "exam.js"
STYLES = ROOT / "cuoti_app" / "static" / "css" / "app.css"


def _source():
    return script_source("exam.js")


def test_explanation_is_lazy_retryable_and_bound_to_current_exam_navigation():
    source = _source()

    assert "function mountExamSelectionExplanation(examId,result)" in source
    assert "if(details.open&&state==='idle')void load()" in source
    assert "`/api/v2/exams/${examId}/selection-explanation`" in source
    assert "new AbortController()" in source
    assert "revision!==examSelectionExplanationRevision" in source
    assert "currentExamPreviewId!==examId" in source
    assert "navigationToken!==window.navigationRevision" in source
    assert "selectionExplanationRetry(body" in source
    assert "resetExamSelectionExplanation();\n  examPage.state.currentExamPreviewId=null;" in source
    assert "window.addEventListener('hashchange'" in source


def test_non_applicable_and_unavailable_states_do_not_fabricate_item_reasons():
    source = _source()

    assert "['available','not_applicable','unavailable'].includes(status)" in source
    assert "if(status!=='available')" in source
    assert "status==='not_applicable'?'本试卷不适用选题依据'" in source
    assert "if(status==='unavailable')selectionExplanationRetry" in source
    assert "payload.items.forEach" in source
    assert "escapeHTML(reason)" in source
    assert "escapeHTML(history)" in source
    assert "escapeHTML(comparison)" in source


def test_alternative_content_is_only_inserted_after_explicit_comparison_expand():
    source = _source()
    alternative_listener = source.index("details.addEventListener('toggle',()=>{")
    alternative_render = source.index("alternativeBody.innerHTML=latexToHtml(content)")
    math_render = source.index("void renderMath(alternativeBody)")

    assert alternative_listener < alternative_render < math_render
    assert "if(!details.open||rendered)return" in source
    assert "escapeHTML(label)" in source
    assert "alternativeBody.className='exam-selection-alternative-content math'" in source


def test_explanation_styles_wrap_on_mobile_without_changing_existing_actions():
    source = _source()
    styles = STYLES.read_text(encoding="utf-8")

    assert "下载PDF" in source
    assert "开始作答" in source
    assert "作答记录" in source
    assert ".exam-selection-explanation{" in styles
    assert ".exam-selection-explanation-item p{" in styles
    assert "grid-template-columns:62px minmax(0,1fr)" in styles
    assert "overflow-wrap:anywhere" in styles
    assert '.exam-selection-alternative-content mjx-container[display="true"]' in styles
    assert "max-width:100%;overflow-x:auto" in styles


def test_exam_review_markers_are_rendered_without_affecting_selection_controls():
    source = _source()
    styles = STYLES.read_text(encoding="utf-8")
    assert "function reviewMarkerBannerHTML(items)" in source
    assert "reviewMarkerBannerHTML(data.items)+latexToHtml(data.content)" in source
    assert "仅作提示，不影响本卷选题与难度。" in source
    assert ".exam-review-markers{" in styles
    assert ".exam-review-marker{" in styles
