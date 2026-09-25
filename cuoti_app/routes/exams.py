"""Exam Blueprint assembly. Endpoint names remain stable across handler moves."""

from flask import Blueprint

from . import exams_candidates, exams_history, exams_access, exams_legacy

bp = Blueprint("exams", __name__)

bp.add_url_rule('/api/mistakes-for-exam', view_func=exams_candidates.api_mistakes_for_exam, methods=['GET'])
bp.add_url_rule('/api/v2/exam-candidates', view_func=exams_candidates.api_v2_exam_candidates, methods=['GET'])
bp.add_url_rule('/api/past-exam-sources', view_func=exams_candidates.api_past_exam_sources, methods=['GET'])
bp.add_url_rule('/internal/jobs/generate-exam', view_func=exams_legacy.api_generate_exam, methods=['POST'])
bp.add_url_rule('/internal/legacy/exams', view_func=exams_history.api_exams, methods=['GET'])
bp.add_url_rule('/api/v2/exams', view_func=exams_history.api_v2_exams, methods=['GET'])
bp.add_url_rule('/api/v2/trash/exams', view_func=exams_history.api_v2_exam_trash, methods=['GET'])
bp.add_url_rule('/api/v2/trash/exams/<int:eid>/restore', view_func=exams_history.api_v2_restore_exam, methods=['POST'])
bp.add_url_rule('/api/v2/exams/<int:eid>', view_func=exams_history.api_v2_exam_detail, methods=['GET', 'DELETE'])
bp.add_url_rule('/api/v2/exams/<int:eid>/selection-explanation', view_func=exams_access.api_v2_exam_selection_explanation, methods=['GET'])
bp.add_url_rule('/pdfs/<path:filename>', view_func=exams_access.serve_pdf, methods=['GET'])
