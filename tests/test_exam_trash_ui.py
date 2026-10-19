from tests.frontend_sources import script_source
from pathlib import Path
from tests.frontend_sources import template_source, application_styles


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cuoti_app" / "static" / "js" / "exam.js"
TEMPLATE = ROOT / "cuoti_app" / "templates" / "index.html"
STYLES = ROOT / "cuoti_app" / "static" / "css" / "app.css"


def _source():
    return script_source("exam.js")


def test_history_and_trash_are_accessible_segmented_views():
    template = template_source()
    styles = application_styles()

    assert 'class="exam-history-tabs" role="tablist"' in template
    assert 'data-history-mode="active" role="tab" aria-selected="true"' in template
    assert 'data-history-mode="trash" role="tab" aria-selected="false"' in template
    assert 'id="exam-history"' in template
    assert 'role="tabpanel" aria-live="polite"' in template
    assert ".exam-history-tabs{" in styles
    assert ".exam-history-tabs button.on{" in styles


def test_switch_aborts_stale_read_resets_paging_and_hides_old_preview():
    source = _source()

    assert "function switchExamHistoryMode(mode)" in source
    assert "examHistoryRequestController.abort()" in source
    assert "examHistoryRequestRevision+=1" in source
    assert "examHistoryItems=[];examHistoryNextCursor=null;examHistoryTotal=0;examHistoryHasMore=false" in source
    assert "mode!==examHistoryMode" in source
    assert "button.setAttribute('aria-selected',String(selected))" in source
    assert "document.querySelector('.exam-history-tabs')?.addEventListener('keydown'" in source
    assert "if(event.key==='ArrowRight')" in source
    assert "hideExamPreview();" in source
    assert "resetExamSelectionExplanation();" in source


def test_trash_uses_restore_only_and_active_rows_remain_readable():
    source = _source()

    assert "mode==='trash'?'/api/v2/trash/exams?':'/api/v2/exams?'" in source
    assert "exam.deleted_at" in source
    assert 'class="exam-history-item is-trash"' in source
    assert 'class="exam-history-title js-view-exam"' in source
    assert 'class="btn btn-out btn-xs js-restore-exam"' in source
    assert 'title="恢复试卷" aria-label="恢复试卷"' in source
    trash_row = source.split("if(mode==='trash')", 1)[1].split("return `<div", 2)[1]
    assert "js-view-exam" not in trash_row.split("`;", 1)[0]
    assert "pdf_url" not in trash_row.split("`;", 1)[0]


def test_soft_delete_copy_and_restore_endpoint_are_explicit():
    source = _source()

    assert 'title="移入回收站" aria-label="移入回收站"' in source
    assert "之后可以从回收站恢复" in source
    assert "此操作不可恢复" not in source[source.index("async function delExam"):]
    assert "await examPage.api.request('/api/v2/exams/'+id,{method:'DELETE'})" in source
    assert "`/api/v2/trash/exams/${id}/restore`" in source
    assert "method:'POST',body:JSON.stringify({})" in source
    assert "showToast('试卷已恢复','success')" in source


def test_history_pagination_remains_shared_without_cross_mode_data():
    source = _source()

    assert "if(append&&examHistoryNextCursor)params.set('after',examHistoryNextCursor)" in source
    assert "examHistoryItems=append?[...examHistoryItems,...items]:items" in source
    assert "page?.has_more&&examHistoryNextCursor" in source
    assert "const mode=examHistoryMode" in source
    assert "revision!==examHistoryRequestRevision||mode!==examHistoryMode" in source


def test_fast_switch_and_restore_refresh_only_history_not_generation_inputs():
    source = _source()
    switch_body = source.split("function switchExamHistoryMode(mode){", 1)[1].split(
        "\nasync function delExam", 1
    )[0]
    restore_body = source.split("async function restoreExam(id,button){", 1)[1].split(
        "\nlet binding=null;", 1
    )[0]

    assert "await loadExamHistory()" in restore_body
    assert "actionMode" not in restore_body
    assert "selectedExamChapters" not in switch_body
    assert "selectedExamMistakeIds" not in switch_body
    assert "switchExamMode" not in switch_body
    assert "gen-status" not in switch_body


def test_history_dates_are_compact_with_full_timestamp_and_no_horizontal_scroll():
    source = _source()
    styles = application_styles()

    assert "function formatExamHistoryDate(value)" in source
    assert 'class="exam-history-time" title="${escapeHTML(exam.created_at||\'\')}"' in source
    assert 'class="exam-history-time" title="${escapeHTML(exam.deleted_at||\'\')}"' in source
    assert "emptyStateHTML('回收站为空',{icon:'trash',compact:true})" in source
    assert "移入回收站的试卷会显示在这里" not in source
    assert "#exam-history{min-width:0;max-width:100%;overflow-x:hidden}" in styles
    assert ".exam-history-time{line-height:1.35;overflow-wrap:anywhere;white-space:normal}" in styles
    assert "grid-template-columns:minmax(0,1fr) minmax(0,98px) 34px" in styles
