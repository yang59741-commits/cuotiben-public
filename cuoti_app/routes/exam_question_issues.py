"""HTTP adaptation for local exam question issue records."""

from flask import jsonify, request
from ..database import get_db
from ..pagination import CursorError, bounded_limit
from ..services.exam_question_issues import IssueError, create_issue, list_issues, update_issue


def _body():
    if not request.is_json:
        raise IssueError('请求体必须为 JSON', status=415)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise IssueError('请求体必须为 JSON 对象')
    return value


def _response(action):
    try:
        return action()
    except IssueError as exc:
        payload = {'error': str(exc), 'code': exc.code}
        if exc.current is not None:
            payload['current'] = exc.current
        return jsonify(payload), exc.status
    except CursorError as exc:
        return jsonify({'error': str(exc), 'code': 'INVALID_CURSOR'}), 400


def api_create_exam_issue(eid):
    def create():
        result = create_issue(get_db(), eid, _body())
        return jsonify(result), 200 if result['replayed'] else 201
    return _response(create)


def api_list_exam_issues():
    def read():
        raw = request.args.get('exam_id')
        exam_id = None
        if raw not in (None, ''):
            try:
                exam_id = int(raw)
                if exam_id <= 0:
                    raise ValueError(raw)
            except ValueError as exc:
                raise IssueError('试卷编号无效') from exc
        return jsonify(list_issues(get_db(), exam_id=exam_id, status=request.args.get('status', ''),
                                  limit=bounded_limit(request.args.get('limit')), after=request.args.get('after')))
    return _response(read)


def api_update_exam_issue(public_id):
    return _response(lambda: jsonify(update_issue(get_db(), public_id, _body())))
