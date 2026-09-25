"""Contracts for the new boundaries, not a repository-wide architecture gate."""

import ast
from importlib.util import resolve_name
from pathlib import Path

from flask import Flask, url_for

from cuoti_app.routes.exams import bp

ROOT = Path(__file__).resolve().parents[1]


def test_exam_blueprint_preserves_urls_methods_and_endpoints():
    app = Flask(__name__)
    app.register_blueprint(bp)
    expected = {
        '/api/mistakes-for-exam': ('api_mistakes_for_exam', {'GET'}),
        '/api/v2/exam-candidates': ('api_v2_exam_candidates', {'GET'}),
        '/api/past-exam-sources': ('api_past_exam_sources', {'GET'}),
        '/internal/jobs/generate-exam': ('api_generate_exam', {'POST'}),
        '/internal/legacy/exams': ('api_exams', {'GET'}),
        '/api/v2/exams': ('api_v2_exams', {'GET'}),
        '/api/v2/trash/exams': ('api_v2_exam_trash', {'GET'}),
        '/api/v2/trash/exams/<int:eid>/restore': ('api_v2_restore_exam', {'POST'}),
        '/api/v2/exams/<int:eid>': ('api_v2_exam_detail', {'GET', 'DELETE'}),
        '/api/v2/exams/<int:eid>/selection-explanation': ('api_v2_exam_selection_explanation', {'GET'}),
        '/pdfs/<path:filename>': ('serve_pdf', {'GET'}),
    }
    actual = {
        rule.rule: (rule.endpoint.removeprefix('exams.'), rule.methods - {'HEAD', 'OPTIONS'})
        for rule in app.url_map.iter_rules() if rule.endpoint.startswith('exams.')
    }
    assert actual == expected
    with app.test_request_context():
        assert url_for('exams.api_v2_exam_detail', eid=12) == '/api/v2/exams/12'
        assert url_for('exams.serve_pdf', filename='exam_12.pdf') == '/pdfs/exam_12.pdf'


def imports(module):
    path = ROOT.joinpath(*module.split('.')).with_suffix('.py')
    package = module.rsplit('.', 1)[0]
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = '.' * node.level + (node.module or '')
            base = resolve_name(base, package) if node.level else base
            yield base
            yield from (base + '.' + alias.name for alias in node.names)


def test_exam_flows_have_no_http_dependencies():
    for path in (ROOT / 'cuoti_app/services/exams').glob('*.py'):
        module = 'cuoti_app.services.exams.' + path.stem
        for dependency in imports(module):
            assert not dependency.startswith(('cuoti_app.routes', 'flask')), (module, dependency)


def test_generation_business_modules_do_not_import_entrypoints():
    modules = [
        'exams.free', 'exams.mistakes', 'exams.draft_cycle', 'exams.completion',
        'exams.legacy_past', 'exam_contracts', 'exam_sources', 'exam_query',
        'exam_generation_policy', 'exam_generation_source_context',
        'exam_generation_raw_papers', 'exam_generation_quality_stage',
        'exam_generation_drafts', 'exam_generation_publish',
    ]
    forbidden = {'cuoti_app.services.exam_generation', 'cuoti_app.services.exams.generation'}
    for name in modules:
        module = 'cuoti_app.services.' + name
        for dependency in imports(module):
            assert not any(dependency == item or dependency.startswith(item + '.') for item in forbidden), (module, dependency)


def test_native_past_modules_do_not_import_legacy_generation():
    modules = [
        'job_handlers_generation', 'exam_generation_past_v3_integration',
        'exam_generation_past_v3_pipeline', 'exam_generation_d2_runtime',
    ]
    forbidden = ('cuoti_app.routes.exams', 'cuoti_app.services.exams.legacy_past')
    for name in modules:
        module = 'cuoti_app.services.' + name
        for dependency in imports(module):
            assert not dependency.startswith(forbidden), (module, dependency)


def test_query_handlers_do_not_import_legacy_generation():
    for name in ('exams_candidates', 'exams_history', 'exams_access'):
        module = 'cuoti_app.routes.' + name
        for dependency in imports(module):
            assert not dependency.startswith(('cuoti_app.routes.exams_legacy', 'cuoti_app.services.exams.legacy_past')), (module, dependency)
