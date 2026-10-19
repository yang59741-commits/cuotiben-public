"""Read-only list attention metadata; full-library counts never read question bodies."""

import json

from . import daily_analysis, mistake_classification, solution_methods


ELIGIBLE = "m.deleted_at IS NULL AND m.content!=''"
PREDICATES = {
    'analysis_missing': "NOT EXISTS (SELECT 1 FROM daily_analysis_items a WHERE a.mistake_id=m.id)",
    'methods_missing': "NOT EXISTS (SELECT 1 FROM mistake_classifications c WHERE c.mistake_id=m.id "
                       "AND json_array_length(COALESCE(c.method_annotations_json,c.methods_json))>0)",
    'confirmation': "(EXISTS (SELECT 1 FROM classification_suggestions s "
                    "WHERE s.mistake_id=m.id AND s.status='pending') OR "
                    "EXISTS (SELECT 1 FROM mistake_classifications c "
                    "WHERE c.mistake_id=m.id AND ((c.method_annotations_json IS NULL "
                    "AND json_array_length(c.methods_json)>0) OR EXISTS "
                    "(SELECT 1 FROM json_each(c.method_annotations_json) j "
                    "WHERE json_extract(j.value,'$.role')='unspecified'))))",
}


def summary(db):
    columns = ','.join(f"COALESCE(SUM({predicate}),0) AS {key}"
                       for key, predicate in PREDICATES.items())
    return dict(db.execute(f"SELECT {columns} FROM mistakes m WHERE {ELIGIBLE}").fetchone())


def date_summary(db, dates):
    if not dates:
        return {}
    rows = db.execute(
        f"SELECT m.date_added,COUNT(*) AS total,SUM({PREDICATES['analysis_missing']}) AS missing "
        f"FROM mistakes m WHERE {ELIGIBLE} AND m.date_added IN (" +
        ','.join('?' for _ in dates) + ') GROUP BY m.date_added', list(dates))
    return {row['date_added']: {'total': row['total'], 'missing': row['missing']} for row in rows}


def item_states(db, items):
    ids = [item['id'] for item in items if item.get('content')]
    if not ids:
        return {}
    placeholders = ','.join('?' for _ in ids)
    cached = daily_analysis._cached_rows(db, ids)
    saved = {row['mistake_id']: dict(row) for row in db.execute(
        f"SELECT c.*,t.active AS type_active FROM mistake_classifications c "
        f"LEFT JOIN question_types t ON t.code=c.primary_type_code WHERE c.mistake_id IN ({placeholders})", ids)}
    pending = {row[0] for row in db.execute(
        f"SELECT DISTINCT mistake_id FROM classification_suggestions "
        f"WHERE status='pending' AND mistake_id IN ({placeholders})", ids)}
    states = {}
    for row in daily_analysis.source_rows_by_ids(db, ids):
        mid = row['id']
        cache, typed = cached.get(mid), saved.get(mid)
        annotations = []
        stale = False
        if typed:
            annotations = solution_methods.normalize_annotations(
                json.loads(typed['methods_json']),
                json.loads(typed['method_annotations_json']) if typed['method_annotations_json'] is not None else None)
            stale = typed['source_hash'] != mistake_classification.source_hash(row) or not typed['type_active']
        unresolved = any(item['role'] == 'unspecified' for item in annotations)
        needs_confirmation = mid in pending or unresolved
        states[row['public_id']] = {
            'analysis': ('missing' if cache is None else
                         'cached' if daily_analysis._cache_is_compatible(cache, row) else 'stale'),
            'methods': 'pending' if needs_confirmation else ('annotated' if annotations else 'missing'),
            'needs_confirmation': needs_confirmation,
            'filters': {'analysis_missing': cache is None, 'methods_missing': not annotations,
                        'confirmation': needs_confirmation},
            'methods_stale': bool(stale),
            'method_annotations': annotations,
        }
    return states


def annotate(db, items):
    states = item_states(db, items)
    for item in items:
        item['attention'] = states.get(item['public_id'], {'analysis': 'unavailable', 'methods': 'unavailable'})
    return date_summary(db, {item['date_added'] for item in items})
