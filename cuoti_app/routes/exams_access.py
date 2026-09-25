"""Selection explanation and published PDF access."""

import re
from flask import abort, current_app, jsonify, send_from_directory
from ..database import get_db

from ..services.exam_query import published_exam_exists


_PDF_NAME = re.compile(r"exam_([1-9]\d*)\.pdf")

def api_v2_exam_selection_explanation(eid):
    from cuoti_app.services.exam_generation_selection_explanation import read_exam_selection_explanation
    from cuoti_app.services.exam_generation_d2_publication import D2PublicationError
    from cuoti_app.services.exam_generation_stage5_d2_solver import D2SolverError
    from cuoti_app.services.exam_generation_stage6_d2_repository import D2RepositoryError

    db = get_db()
    if not published_exam_exists(db, eid):
        return jsonify({'error': '试卷不存在'}), 404
    try:
        explanation = read_exam_selection_explanation(db, exam_id=eid)
    except (D2PublicationError, D2RepositoryError, D2SolverError, ValueError, KeyError, TypeError) as exc:
        current_app.logger.warning("Exam selection explanation unavailable: exam_id=%s, error_type=%s", eid, type(exc).__name__)
        explanation = {"status": "unavailable", "items": [], "summary_text": "本次未能完整读取选题依据，试卷与PDF不受影响。", "scope_text": "未用当前题库或新模型判断替代原选题记录。"}
    return jsonify(explanation)


def serve_pdf(filename):
    match = _PDF_NAME.fullmatch(filename)
    if not match:
        abort(404)
    exam_id = int(match.group(1))
    exists = published_exam_exists(get_db(), exam_id)
    if not exists:
        abort(404)
    pdf_dir = current_app.config['PDF_DIR']
    return send_from_directory(pdf_dir, filename, mimetype='application/pdf')
