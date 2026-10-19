from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "cuoti_app" / "static"
JS = STATIC / "js"
REVIEW_TEMPLATE = (
    ROOT / "cuoti_app" / "templates" / "partials" / "pages" / "review.html"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_review_page_has_two_independent_accessible_panels():
    template = read(REVIEW_TEMPLATE)
    assert 'class="tabs review-mode-tabs" role="tablist"' in template
    assert 'id="review-tab-spaced" role="tab" aria-label="间隔复习"' in template
    assert 'id="review-tab-daily" role="tab" aria-label="每日自主复习"' in template
    assert 'aria-selected="true" aria-controls="review-spaced-panel"' in template
    assert 'aria-selected="false" aria-controls="review-daily-panel"' in template
    assert 'id="review-spaced-panel"' in template
    assert 'id="review-daily-panel"' in template
    for element_id in (
        "spaced-review-subject",
        "spaced-review-chapter",
        "spaced-review-export-pdf",
        "spaced-review-list",
        "spaced-review-load-more",
        "daily-review-subject",
        "daily-review-generate-pdf",
        "daily-review-list",
    ):
        assert f'id="{element_id}"' in template
    assert 'id="review-list"' not in template
    assert 'id="review-export-pdf"' not in template


def test_one_review_lifecycle_loads_both_controllers_and_routes_by_hash():
    loader = read(JS / "feature-loader.js")
    page = read(JS / "review.js")
    daily_facade = read(JS / "daily-review.js")
    spaced_index = loader.index("/static/js/review/controller.js")
    daily_index = loader.index("/static/js/daily-review/controller.js")
    page_index = loader.index("/static/js/review.js")
    assert spaced_index < daily_index < page_index
    assert page.count("features.register('review'") == 1
    assert "features.register('review'" not in daily_facade
    assert "params?.get?.('mode')==='daily'?'daily':'spaced'" in page
    assert "new URLSearchParams({mode})" in page
    assert "activeMode==='daily'" in page
    assert "daily.unmount()" in page
    assert "spaced.deactivate()" in page
    assert "ArrowRight" in page and "ArrowLeft" in page


def test_review_controllers_cancel_reads_without_cancelling_pdf_jobs():
    spaced = read(JS / "review" / "controller.js")
    daily = read(JS / "daily-review" / "controller.js")
    assert "state.requestController?.abort()" in spaced
    assert "state.chapterRequestController?.abort()" in spaced
    assert "revision!==state.requestRevision" in spaced
    assert "function mergeUniqueReviewItems(current,incoming)" in spaced
    assert "state.requestController?.abort()" in daily
    assert "revision!==state.requestRevision" in daily
    assert "runJobRequest('mistake-export'" in spaced
    assert "export_context:'selected_mistakes'" in spaced
    assert "const filters={review_due:true}" in spaced
    assert "runJobRequest('mistake-export'" in daily
    assert "export_context:'daily_review'" in daily
    for source in (spaced, daily):
        deactivate = source[source.index("function deactivate()") :]
        assert "exportInFlight=false" not in deactivate
        assert "generationInFlight=false" not in deactivate


def test_statistics_links_scoped_card_but_keeps_global_badge():
    core = read(JS / "core.js")
    stats = "\n".join(read(path) for path in [JS / "stats.js", *sorted((JS / "stats").glob("*.js"))])
    index = read(ROOT / "cuoti_app" / "templates" / "index.html")
    assert "api('/api/v2/stats/reminders')" in core
    assert "window.Cuoti.reviewBadge" in core
    assert "'open-spaced-review'" in core
    assert "new URLSearchParams({mode:'spaced'})" in core
    assert "const reminderParams=new URLSearchParams()" in stats
    assert 'data-action="open-spaced-review"' in stats
    assert "今日间隔复习" in stats
    assert "reviewBadge?.refresh?.()" in stats
    assert 'data-page="review" data-action="navigate" aria-label="复习"' in index


def test_service_worker_tracks_both_lazy_review_bundles():
    worker = read(STATIC / "sw.js")
    for asset in (
        "/static/js/review/controller.js",
        "/static/js/review.js",
        "/static/js/daily-review/controller.js",
        "/static/js/daily-review.js",
    ):
        assert asset in worker


def test_daily_review_page_leads_with_progress_and_moves_generation_to_drawer():
    template = read(REVIEW_TEMPLATE)
    daily = template[template.index('id="review-daily-panel"') :]
    assert daily.index('id="daily-batch-card"') < daily.index('id="daily-remediation"')
    assert daily.index('id="daily-remediation"') < daily.index('id="daily-review-list"')
    assert 'id="daily-more-menu" role="menu"' in daily
    assert 'id="daily-drawer" role="dialog" aria-modal="true"' in daily
    for view in ("generate", "remediation", "summary"):
        assert f'data-drawer-view="{view}"' in daily
    assert daily.index('data-drawer-view="generate"') < daily.index('id="daily-review-generate-pdf"')
    assert 'id="daily-result-chips" role="group"' in daily
    assert 'id="daily-undo" role="status"' in daily
    # Removed controls: the hidden assistance checkbox, collapse toggle and result dropdown.
    for removed in ('id="daily-collapse-done"', 'id="daily-group-types"', 'id="daily-view-result"', "<details"):
        assert removed not in daily


def test_daily_review_records_with_undo_window_without_full_reload():
    controller = read(JS / "daily-review" / "controller.js")
    assert "const UNDO_DELAY=5000" in controller
    assert "undoTimer=setTimeout(()=>{void flushQueued()},UNDO_DELAY)" in controller
    assert "choice==='solo'?'none':choice==='hint'?'hint':(hintUsed(item)?'hint':'none')" in controller
    submit = controller[controller.index("async function submitRecord") : controller.index("/* ---------- hints")]
    assert "renderCard(item.id)" in submit
    assert "void load()" in submit  # conflict refresh waits for the write to settle
    assert submit.count("load(") == 1
    load = controller[controller.index("async function load()") :]
    assert load.index("await flushQueued()") < load.index("state.requestController?.abort()")
    deactivate = controller[controller.index("function deactivate()") :]
    assert "void flushQueued()" in deactivate
    assert "window.addEventListener('pagehide'" in controller


def test_daily_review_keyboard_shortcuts_are_scoped_to_the_visible_panel():
    controller = read(JS / "daily-review" / "controller.js")
    keydown = controller[controller.index("function onKeydown(event)") : controller.index("function relocate(")]
    assert "panel.hidden||!el('p-review')?.classList.contains('on')" in keydown
    assert "isTypingTarget(event.target)" in keydown
    assert "/^[1-4]$/.test(key)" in keydown
    for key in ("'h'", "'j'", "'k'", "'z'"):
        assert f"key==={key}" in keydown
    assert "trapDrawerFocus(event)" in keydown


def test_daily_review_filters_whole_batch_on_client_to_keep_numbers_stable():
    controller = read(JS / "daily-review" / "controller.js")
    assert "feature.api.current('',state.requestController.signal,{},state.sessionId)" in controller
    assert "function matchesFilters(item)" in controller
    assert "index:state.items.indexOf(item)+1" in controller
