"""Taxonomy editing, classification confirmation and explicit backfill."""

from flask import Blueprint, jsonify, request

from ..database import get_db
from ..services import mistake_classification as service
from ..services import solution_methods
from ..validation import ValidationError, json_object
from .daily_review import _coordinator, _job_response

bp = Blueprint('classifications', __name__)


@bp.errorhandler(service.ClassificationError)
def classification_error(exc):
    return jsonify({'error': str(exc), 'code': 'CLASSIFICATION_ERROR'}), exc.status


@bp.errorhandler(ValidationError)
def validation_error(exc):
    return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400


@bp.get('/api/v2/question-types')
def question_types():
    items = service.list_types(get_db(), subject=request.args.get('subject'), include_inactive=request.args.get('all') == '1')
    chapter = request.args.get('chapter')
    if chapter:
        items = [item for item in items if not item['chapters'] or chapter in item['chapters']]
    return jsonify({'items': items})


@bp.get('/api/v2/solution-methods')
def method_catalog():
    return jsonify({'version': solution_methods.CATALOG_VERSION,
                    'items': solution_methods.list_methods(request.args.get('subject'))})


@bp.post('/api/v2/question-types')
@bp.put('/api/v2/question-types/<code>')
def save_question_type(code=None):
    code = service.update_type(get_db(), code, json_object(request))
    return jsonify({'code': code})


@bp.get('/api/v2/mistakes/<public_id>/classification')
def get_classification(public_id):
    db = get_db()
    row = service.mistake_row(db, public_id)
    return jsonify({**service.get_classification(db, row), 'question': row['content'], 'qnum': row['qnum']})


@bp.put('/api/v2/mistakes/<public_id>/classification')
def save_classification(public_id):
    return jsonify(service.update_classification(get_db(), public_id, json_object(request)))


@bp.get('/api/v2/classification-suggestions')
def suggestions():
    db = get_db()
    items = []
    for row in db.execute("SELECT s.id,m.public_id,m.content,m.qnum,su.name AS subject FROM classification_suggestions s "
        "JOIN mistakes m ON m.id=s.mistake_id LEFT JOIN subjects su ON su.id=m.subject_id "
        "WHERE s.status='pending' AND m.deleted_at IS NULL ORDER BY s.id DESC LIMIT 200"):
        item = dict(row)
        item['classification'] = service.get_classification(db, service.mistake_row(db, row['public_id']))
        items.append(item)
    return jsonify({'items': items})


@bp.post('/api/v2/classification-suggestions/<int:suggestion_id>/resolve')
def resolve_suggestion(suggestion_id):
    return jsonify(service.resolve_suggestion(get_db(), suggestion_id, json_object(request)))


@bp.post('/api/v2/mistake-classifications/backfill/preview')
def backfill_preview():
    return jsonify(service.backfill_preview(get_db(), json_object(request)))


@bp.post('/api/v2/mistake-classifications/backfill')
def backfill():
    body = json_object(request)
    preview = service.backfill_preview(get_db(), body.get('scope') or {})
    if preview['fingerprint'] != body.get('fingerprint'):
        raise service.ClassificationError('补标范围已变化，请重新预览', 409)
    if not preview['targets']:
        raise service.ClassificationError('当前范围无需补标')
    job, created = _coordinator().submit('classification', {'operation': 'backfill', 'targets': preview['targets']},
                                       idempotency_key=str(body.get('request_id') or preview['fingerprint']))
    return _job_response(job, 202 if created else 200)
