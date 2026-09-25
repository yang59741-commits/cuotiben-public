"""Exam history, detail and trash HTTP adaptation."""

import re
from flask import current_app, jsonify, request
from ..database import get_db
from ..pagination import CursorError, bounded_limit
from ..services.exam_query import (
    ExamListFilters,
    delete_published_exam,
    list_deleted_exams,
    list_exams,
    restore_published_exam,
)

from ..services.exam_contracts import positive_int as _positive_int
from ..services.exam_query import (
    list_legacy_http_exams,
    get_http_exam_detail,
    published_exam_exists,
)
from ..services.exam_sources import safe_error_text


def api_exams():
    return jsonify(list_legacy_http_exams(get_db()))

def api_v2_exams():
    return _exam_list_response()


def api_v2_exam_trash():
    return _exam_list_response(trashed=True)


def _exam_list_response(*, trashed=False):
    db = get_db()
    try:
        limit = bounded_limit(request.args.get("limit"))
        title = str(request.args.get("title") or "").strip()
        if len(title) > 100:
            raise ValueError("标题筛选过长")
        subject_id = request.args.get("subject_id")
        if subject_id not in (None, ""):
            subject_id = _positive_int(subject_id, "科目 ID")
        else:
            subject_id = None
        source = str(request.args.get("source") or "").strip()
        if len(source) > 40:
            raise ValueError("来源筛选过长")
        date_from = str(request.args.get("date_from") or "").strip()
        date_to = str(request.args.get("date_to") or "").strip()
        for label, value in (("开始日期", date_from), ("结束日期", date_to)):
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{label}格式无效")
        if date_from and date_to and date_from > date_to:
            raise ValueError("开始日期不能晚于结束日期")
        filters = {
            "title": title, "subject_id": subject_id, "source": source,
            "date_from": date_from, "date_to": date_to,
        }
        reader = list_deleted_exams if trashed else list_exams
        return jsonify(reader(
            db, ExamListFilters(**filters), limit=limit,
            after=request.args.get("after"),
        ))
    except CursorError as exc:
        return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


def api_v2_restore_exam(eid):
    db = get_db()
    try:
        restored = restore_published_exam(db, eid)
    except Exception as exc:
        return jsonify({'error': f'恢复试卷失败: {_safe_error_text(exc)}'}), 500
    if not restored:
        return jsonify({'error': '回收站中没有该试卷'}), 404
    return jsonify({'ok': True, 'exam_id': eid})


def api_v2_exam_detail(eid):
    db = get_db()
    if request.method == 'DELETE':
        if not published_exam_exists(db, eid):
            return jsonify({'error': '试卷不存在'}), 404
        try:
            deleted = delete_published_exam(db, current_app.config['PDF_DIR'], eid)
        except Exception as exc:
            return jsonify({'error': f'移入回收站失败: {_safe_error_text(exc)}'}), 500
        if not deleted:
            return jsonify({'error': '试卷不存在'}), 404
        return jsonify({'ok': True, 'exam_id': eid})
    detail = get_http_exam_detail(db, eid)
    if detail is None:
        return jsonify({'error': '试卷不存在'}), 404
    return jsonify(detail)

def _safe_error_text(error):
    return safe_error_text(
        error, secrets=(str(current_app.config.get('DEEPSEEK_API_KEY') or ''),),
    )
