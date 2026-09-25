"""Exam candidate and source HTTP queries."""

from pathlib import Path
from flask import current_app, jsonify, request
from ..database import get_db
from ..pagination import CursorError, bounded_limit
from ..services.past_exams import PastExamSourceError, discover_past_exam_sources, public_source
from ..services.past_exam_profile_activation_store import ProfileActivationStoreError
from ..services.past_exam_profile_availability import (
    load_d2_coverage,
    load_profile_availability_snapshot,
    overlay_legacy_source_availability,
)
from ..services.past_exam_profile_v3 import ProfileV3ContractError

from ..services.exam_contracts import positive_int as _positive_int
from ..services.exam_query import (
    list_exam_candidates,
    subject_ids_by_name,
    list_legacy_exam_candidates,
)


def api_mistakes_for_exam():
    db = get_db()
    where, params = ["m.status='未掌握'", "m.deleted_at IS NULL"], []
    if request.args.get('subject_id'):
        try:
            subject_id = _positive_int(request.args['subject_id'], "科目 ID")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        where.append("m.subject_id=?"); params.append(subject_id)
    if request.args.get('chapter_id'):
        try:
            chapter_id = _positive_int(request.args['chapter_id'], "章节 ID")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        where.append("m.chapter_id=?"); params.append(chapter_id)
    return jsonify(list_legacy_exam_candidates(db, where=where, params=params))


def _exam_candidate_filters(args):
    where, params = ["m.status='未掌握'", "m.deleted_at IS NULL"], []
    filters = {"status": "未掌握"}
    for key, label in (("subject_id", "科目 ID"), ("chapter_id", "章节 ID")):
        if args.get(key):
            value = _positive_int(args[key], label)
            where.append(f"m.{key}=?")
            params.append(value)
            filters[key] = value
    return where, params, filters


def api_v2_exam_candidates():
    try:
        limit = bounded_limit(request.args.get("limit"))
        where, params, filters = _exam_candidate_filters(request.args)
        return jsonify(list_exam_candidates(
            get_db(), where=where, params=params, filters=filters,
            limit=limit, after=request.args.get("after"),
        ))
    except (ValueError, CursorError) as exc:
        return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400

def api_past_exam_sources():
    db = get_db()
    subject_ids = subject_ids_by_name(db)
    if not str(current_app.config.get('PAST_EXAM_DIR') or '').strip():
        return jsonify(
            {
                'error': (
                    '未配置真题 TeX 目录；请设置 CUOTI_PAST_EXAM_DIR '
                    '或 instance/settings.json 的 past_exam_dir'
                )
            }
        ), 503
    try:
        sources = discover_past_exam_sources(current_app.config['PAST_EXAM_DIR'])
        availability = load_profile_availability_snapshot(db)
        d2_coverage = load_d2_coverage(
            Path(current_app.config['DATABASE']).parent / 'runtime' / 'd2-expansion-coverage.json'
        )
        return jsonify(
            [
                overlay_legacy_source_availability(
                    public_source(source, subject_ids[source['subject']]),
                    availability['registry'],
                    availability['artifact_map'],
                    evidence_directory=Path(current_app.config['DATABASE']).parent / 'runtime' / 'profile_expansion_evidence',
                    d2_coverage=d2_coverage,
                )
                for source in sources
                if source['subject'] in subject_ids
                and len(source.get('exam_sections') or []) >= 1
            ]
        )
    except ProfileActivationStoreError as exc:
        return jsonify(
            {
                'error': str(exc),
                'code': exc.code,
                'retryable': exc.retryable,
            }
        ), 503 if exc.retryable else 500
    except ProfileV3ContractError as exc:
        return jsonify(
            {
                'error': str(exc),
                'code': exc.code,
                'reason': exc.details.get('reason', exc.code),
                'retryable': False,
            }
        ), 409
    except PastExamSourceError as exc:
        return jsonify({'error': str(exc)}), 500
