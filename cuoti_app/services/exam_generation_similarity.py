"""Local near-duplicate evidence; never candidate exclusion or utility scores."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from fractions import Fraction
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .exam_repetition_policy import normalize_question_content
from .semantic_recall_terms import canonical_concept, UNKNOWN_CONCEPT_SENTINELS


SIMILARITY_VERSION = 'd2-near-duplicate-evidence-v3'
SIMILARITY_FREEZE_VERSION = 'd2-near-duplicate-freeze-v1'
_UNKNOWN_CONCEPTS = frozenset(map(canonical_concept, UNKNOWN_CONCEPT_SENTINELS))
_TEX_TOKENS = re.compile(r'\\[A-Za-z]+|\\.|[^\\]+', re.DOTALL)
_LAYOUT_COMMANDS = frozenset((
    r'\(', r'\)', r'\[', r'\]', r'\,', r'\;', r'\:', r'\!', '\\ ',
    r'\quad', r'\qquad', r'\enspace', r'\thinspace',
    r'\left', r'\right', r'\big', r'\Big', r'\bigg', r'\Bigg',
    r'\bigl', r'\bigr', r'\Bigl', r'\Bigr', r'\biggl', r'\biggr',
    r'\Biggl', r'\Biggr', r'\displaystyle', r'\textstyle',
    r'\scriptstyle', r'\scriptscriptstyle',
))
_EQUIVALENT_COMMANDS = {
    r'\dfrac': r'\frac', r'\tfrac': r'\frac',
    r'\le': r'\leq', r'\leqslant': r'\leq',
    r'\ge': r'\geq', r'\geqslant': r'\geq',
    r'\ne': r'\neq', r'\to': r'\rightarrow',
}
_EXAMPLE_LABEL = re.compile(
    r'\A\s*(?:【\s*例\s*\d+(?:\s*[.．]\s*\d+)*\s*】'
    r'|例\s*\d+(?:\s*[.．]\s*\d+)*(?:\s*[.．:：])?)\s*'
)
_SCORE_LABEL = re.compile(r'\A\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*')
_LEADING_CITATION = re.compile(r'\A\s*[（(]([^（）()\r\n]+)[）)]\s*')
_TRAILING_CITATION = re.compile(r'\s*[（(]([^（）()\r\n]+)[）)]\s*\Z')
_INSTITUTION_LABEL = re.compile(r'[\u3400-\u9fff]{2,30}(?:大学|学院|统考)')


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _strip_source_labels(text: str, source_labels: Iterable[str]) -> str:
    institutions = {label.strip() for label in source_labels
                    if isinstance(label, str) and _INSTITUTION_LABEL.fullmatch(label.strip())}

    def is_source_citation(value):
        parts = [part.strip() for part in re.split(r'[,，;；、]', value)]
        return any(part in institutions for part in parts) and all(
            part in institutions or re.fullmatch(r'(?:19|20)\d{2}\s*年', part)
            for part in parts
        )

    text = _EXAMPLE_LABEL.sub('', text, count=1)
    text = _SCORE_LABEL.sub('', text, count=1)
    # Only boundary annotations backed by the input's source metadata qualify.
    # Parenthesized conditions inside the question are never removed.
    for pattern in (_LEADING_CITATION, _TRAILING_CITATION):
        match = pattern.search(text)
        if match is not None and is_source_citation(match.group(1)):
            text = text[:match.start()] + text[match.end():]
    return text


def normalize_similarity_text(value: object, *, source_labels: Iterable[str] = ()) -> str:
    """Canonicalize layout only; retain math groups, fonts, operators and rows."""
    def replace_token(match):
        token = match.group()
        if token in _LAYOUT_COMMANDS:
            return ''
        if token.startswith('\\'):
            return _EQUIVALENT_COMMANDS.get(token, token)
        return token.replace('~', '')

    text = _strip_source_labels(str(value or ''), source_labels)
    return normalize_question_content(_TEX_TOKENS.sub(replace_token, text))


@dataclass(frozen=True)
class SimilarityQuestion:
    content_hash: str
    text: str
    knowledge_points: frozenset[str]
    methods: frozenset[str]
    question_type_family: str


@dataclass(frozen=True)
class PairSimilarity:
    status: str
    semantic_bps: int | None
    text_bps: int | None


@dataclass(frozen=True)
class SimilarityThresholds:
    semantic_bps: int
    text_bps: int


def calibrated_similarity_rule(subject: str) -> dict:
    # Reviewed 48-pair calibration and fixed validation: see the 20260910 audit.
    semantic, text = {
        '数学分析': (7600, 8791),
        '高等代数': (7000, 8818),
    }[subject]
    return {
        'version': SIMILARITY_VERSION,
        'calibration_id': 'd2-reviewed-48-pairs-20260910',
        'subject': subject,
        'thresholds': {'semantic_bps': semantic, 'text_bps': text},
    }


def prepare_similarity_question(
    *, content_hash: str, content: str, knowledge_points: Iterable[str],
    methods: Iterable[str], question_type_family: str, source_labels: Iterable[str] = (),
) -> SimilarityQuestion:
    def known_concepts(values):
        return frozenset(c for c in map(canonical_concept, values) if c and c not in _UNKNOWN_CONCEPTS)

    family = canonical_concept(question_type_family)
    return SimilarityQuestion(
        content_hash=content_hash,
        text=normalize_similarity_text(content, source_labels=source_labels),
        knowledge_points=known_concepts(knowledge_points),
        methods=known_concepts(methods),
        question_type_family='' if family in _UNKNOWN_CONCEPTS else family,
    )


def compare_similarity_questions(left: SimilarityQuestion, right: SimilarityQuestion) -> PairSimilarity:
    if left.content_hash and left.content_hash == right.content_hash:
        return PairSimilarity('exact_content', 10000, 10000)
    if not all((left.text, right.text, left.knowledge_points, right.knowledge_points,
                left.methods, right.methods, left.question_type_family, right.question_type_family)):
        return PairSimilarity('unknown_incomplete_evidence', None, None)
    # A malformed frozen snapshot is evidence we cannot compare, not a reason
    # to abort the whole candidate preflight.  Normal inputs are strings; this
    # branch keeps legacy/hand-authored receipts explicitly unknown.
    if not isinstance(left.text, str) or not isinstance(right.text, str):
        return PairSimilarity('unknown_incomplete_evidence', None, None)
    knowledge = Fraction(len(left.knowledge_points & right.knowledge_points),
                         len(left.knowledge_points | right.knowledge_points))
    methods = Fraction(len(left.methods & right.methods), len(left.methods | right.methods))
    # These are the existing structured-similarity weights, not selection weights.
    semantic = (5 * knowledge + 3 * methods + 2 * (left.question_type_family == right.question_type_family)) / 10
    # Reuse one matcher for the two required directions.  Besides avoiding
    # needless allocator churn when a task compares thousands of pairs, this
    # keeps the mandated standard-library SequenceMatcher/autojunk=False
    # calculation exactly unchanged.
    matcher = SequenceMatcher(autojunk=False)
    text_values = []
    for first, second in ((left.text, right.text), (right.text, left.text)):
        matcher.set_seqs(first, second)
        matches = sum(block.size for block in matcher.get_matching_blocks())
        text_values.append(20000 * matches // (len(first) + len(second)))
    return PairSimilarity('available', semantic.numerator * 10000 // semantic.denominator, min(text_values))


def _snapshot_text(candidate: Mapping[str, Any]) -> str | None:
    raw = candidate.get('content_snapshot')
    try:
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get('content'), str):
        return None
    parts = [snapshot['content']]
    for field in ('conditions', 'figure_references', 'options'):
        values = snapshot.get(field, ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return None
        if any(not isinstance(value, str) for value in values):
            return None
        parts.extend(values)
    return '\n'.join(parts)


def _card_for(card_facts: Mapping[Any, Any], candidate: Mapping[str, Any]) -> Mapping[str, Any] | None:
    key = (candidate.get('source_kind'), candidate.get('candidate_id'))
    card = card_facts.get(key, card_facts.get(candidate.get('candidate_id')))
    return card if isinstance(card, Mapping) else None


def _identity_row(candidate: Mapping[str, Any], edge: Mapping[str, Any], question: SimilarityQuestion | None) -> dict[str, Any]:
    result = {
        'candidate_id': edge.get('candidate_id', candidate.get('candidate_id')),
        'candidate_fact_hash': edge.get('candidate_fact_hash'),
        'content_hash': candidate.get('content_hash'),
        'card_version_id': candidate.get('card_version_id'),
        'card_hash': candidate.get('card_hash'),
        'content_snapshot_hash': candidate.get('content_snapshot_hash'),
        'status': 'available' if question is not None else 'unknown_incomplete_evidence',
        'normalized_text': question.text if question is not None else None,
        'knowledge_points': sorted(question.knowledge_points) if question is not None else None,
        'methods': sorted(question.methods) if question is not None else None,
        'question_type_family': question.question_type_family if question is not None else None,
    }
    return result


def _pair_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple:
    return (left['candidate_id'], left.get('content_hash'), right['candidate_id'], right.get('content_hash'))


def _question_from_frozen_row(row: Mapping[str, Any]) -> SimilarityQuestion | None:
    """Rehydrate only the local, already-frozen comparison facts."""
    if row.get('status') != 'available':
        return None
    text = row.get('normalized_text')
    knowledge = row.get('knowledge_points')
    methods = row.get('methods')
    family = row.get('question_type_family')
    if (
        not isinstance(text, str) or not text
        or not isinstance(knowledge, list) or not knowledge
        or not isinstance(methods, list) or not methods
        or not isinstance(family, str) or not family
        or any(not isinstance(value, str) or not value for value in (*knowledge, *methods))
    ):
        return None
    return SimilarityQuestion(
        content_hash=str(row.get('content_hash') or ''),
        text=text,
        knowledge_points=frozenset(knowledge),
        methods=frozenset(methods),
        question_type_family=family,
    )


def _frozen_pair_row(left: Mapping[str, Any], right: Mapping[str, Any], *, thresholds: SimilarityThresholds) -> dict[str, Any]:
    left_question = _question_from_frozen_row(left)
    right_question = _question_from_frozen_row(right)
    comparison = (
        compare_similarity_questions(left_question, right_question)
        if left_question is not None and right_question is not None
        else PairSimilarity('unknown_incomplete_evidence', None, None)
    )
    return {
        'left_candidate_id': left['candidate_id'],
        'right_candidate_id': right['candidate_id'],
        'left_content_hash': left.get('content_hash'),
        'right_content_hash': right.get('content_hash'),
        **asdict(comparison),
        'near_duplicate': is_near_duplicate(comparison, thresholds),
    }


def build_similarity_freeze(
    *,
    subject: str,
    candidates: Sequence[Mapping[str, Any]],
    graph_edges: Sequence[Mapping[str, Any]],
    card_facts: Mapping[Any, Any] | None = None,
    history_candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a compact task-local receipt for only graph/history pairs.

    Pair results, not a full-catalog matrix, are frozen. Missing card or text
    evidence remains explicit unknown and never becomes a negative match.
    """
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError('similarity_subject_invalid')
    card_facts = card_facts or {}
    thresholds = SimilarityThresholds(**calibrated_similarity_rule(subject)['thresholds'])
    edge_by_id: dict[str, Mapping[str, Any]] = {}
    for edge in graph_edges:
        if not isinstance(edge, Mapping):
            continue
        edge_by_id.setdefault(str(edge.get('candidate_id')), edge)
    current_rows: list[dict[str, Any]] = []
    current_questions: dict[str, SimilarityQuestion | None] = {}
    for candidate_id, edge in sorted(edge_by_id.items()):
        candidate = next((row for row in candidates if row.get('candidate_id') == candidate_id), None)
        if candidate is None:
            continue
        text = _snapshot_text(candidate)
        card = _card_for(card_facts, candidate)
        question = None
        if text is not None and card is not None:
            question = prepare_similarity_question(
                content_hash=str(candidate.get('content_hash') or ''), content=text,
                knowledge_points=card.get('knowledge_points', ()), methods=card.get('methods', ()),
                question_type_family=card.get('question_type', candidate.get('question_type_family', '')),
                source_labels=card.get('source_constraints', ()),
            )
            if not question.knowledge_points or not question.methods or not question.question_type_family:
                question = None
        current_questions[candidate_id] = question
        current_rows.append(_identity_row(candidate, edge, question))

    pairs: list[dict[str, Any]] = []
    for index, left_id in enumerate(sorted(current_questions)):
        for right_id in sorted(current_questions)[index + 1:]:
            left_row = next(x for x in current_rows if x['candidate_id'] == left_id)
            right_row = next(x for x in current_rows if x['candidate_id'] == right_id)
            pairs.append(_frozen_pair_row(left_row, right_row, thresholds=thresholds))

    history_rows: list[dict[str, Any]] = []
    historical_usage: dict[str, dict[str, int]] = {}
    for index, raw in enumerate(history_candidates, start=1):
        if not isinstance(raw, Mapping):
            continue
        text = _snapshot_text(raw)
        card = _card_for(card_facts, raw)
        question = None
        if text is not None and card is not None:
            question = prepare_similarity_question(
                content_hash=str(raw.get('content_hash') or ''), content=text,
                knowledge_points=card.get('knowledge_points', ()), methods=card.get('methods', ()),
                question_type_family=card.get('question_type', raw.get('question_type_family', '')),
                source_labels=card.get('source_constraints', ()),
            )
            if not question.knowledge_points or not question.methods or not question.question_type_family:
                question = None
        history_rows.append({
            'candidate_id': raw.get('candidate_id'), 'content_hash': raw.get('content_hash'),
            'source_kind': raw.get('source_kind'), 'order_rank': raw.get('order_rank', index),
            'status': 'available' if question is not None else 'unknown_incomplete_evidence',
            'normalized_text': question.text if question is not None else None,
            'knowledge_points': sorted(question.knowledge_points) if question is not None else None,
            'methods': sorted(question.methods) if question is not None else None,
            'question_type_family': question.question_type_family if question is not None else None,
        })
        if question is None:
            continue
        for current_id, current_question in current_questions.items():
            if current_question is None:
                continue
            comparison = compare_similarity_questions(current_question, question)
            if comparison.status != 'available' or raw.get('content_hash') == next(x['content_hash'] for x in current_rows if x['candidate_id'] == current_id):
                continue
            if not is_near_duplicate(comparison, thresholds):
                continue
            entry = historical_usage.setdefault(current_id, {'success_count': 0, 'latest_order_rank': 0})
            entry['success_count'] += 1
            entry['latest_order_rank'] = max(entry['latest_order_rank'], int(raw.get('order_rank', index)))

    historical_near_usage = {
        candidate_id: value for candidate_id, value in sorted(historical_usage.items())
    }
    body = {
        'version': SIMILARITY_FREEZE_VERSION,
        'rule': calibrated_similarity_rule(subject),
        'subject': subject,
        'candidate_inputs': sorted(current_rows, key=lambda row: row['candidate_id']),
        'pairs': sorted(pairs, key=lambda row: (row['left_candidate_id'], row['right_candidate_id'])),
        'history_inputs': history_rows,
        'historical_near_usage': historical_near_usage,
    }
    return {**body, 'freeze_hash': _hash(body)}


def validate_similarity_freeze(
    receipt: Mapping[str, Any], *, graph: Mapping[str, Any], subject: str,
) -> dict[str, Any]:
    """Validate a frozen receipt against the graph identities without re-comparing text."""
    if not isinstance(receipt, Mapping) or set(receipt) != {
        'version', 'rule', 'subject', 'candidate_inputs', 'pairs', 'history_inputs',
        'historical_near_usage', 'freeze_hash',
    }:
        raise ValueError('similarity_freeze_shape_invalid')
    body = {key: receipt[key] for key in receipt if key != 'freeze_hash'}
    if receipt.get('version') != SIMILARITY_FREEZE_VERSION or receipt.get('subject') != subject or _hash(body) != receipt.get('freeze_hash'):
        raise ValueError('similarity_freeze_hash_invalid')
    rule = receipt.get('rule')
    if (
        not isinstance(rule, Mapping)
        or rule.get('version') != SIMILARITY_VERSION
        or rule.get('subject') != subject
        or not isinstance(rule.get('calibration_id'), str)
        or not isinstance(rule.get('thresholds'), Mapping)
        or type(rule['thresholds'].get('semantic_bps')) is not int
        or type(rule['thresholds'].get('text_bps')) is not int
        or not 0 <= rule['thresholds']['semantic_bps'] <= 10000
        or not 0 <= rule['thresholds']['text_bps'] <= 20000
    ):
        raise ValueError('similarity_rule_invalid')
    candidate_rows = receipt.get('candidate_inputs')
    pair_rows = receipt.get('pairs')
    history_rows = receipt.get('history_inputs')
    usage = receipt.get('historical_near_usage')
    if not all(isinstance(value, list) for value in (candidate_rows, pair_rows, history_rows)) or not isinstance(usage, Mapping):
        raise ValueError('similarity_freeze_members_invalid')
    candidates = graph.get('edges', ()) if isinstance(graph, Mapping) else ()
    expected = {
        (edge.get('candidate_id'), edge.get('candidate_fact_hash'), edge.get('content_hash'))
        for edge in candidates if isinstance(edge, Mapping)
    }
    inputs = {
        (row.get('candidate_id'), row.get('candidate_fact_hash'), row.get('content_hash'))
        for row in candidate_rows if isinstance(row, Mapping)
    }
    if not expected.issubset(inputs):
        raise ValueError('similarity_freeze_graph_coverage_invalid')
    if any(not isinstance(row, Mapping) or not isinstance(row.get('candidate_id'), str) for row in candidate_rows):
        raise ValueError('similarity_freeze_candidate_identity_invalid')
    candidate_by_id = {row['candidate_id']: row for row in candidate_rows}
    if len(candidate_by_id) != len(candidate_rows):
        raise ValueError('similarity_freeze_candidate_duplicate')
    ids = set(candidate_by_id)
    pair_keys: set[tuple[str, str]] = set()
    thresholds = SimilarityThresholds(**rule['thresholds'])
    for pair in pair_rows:
        if not isinstance(pair, Mapping) or pair.get('left_candidate_id') not in ids or pair.get('right_candidate_id') not in ids:
            raise ValueError('similarity_freeze_pair_identity_invalid')
        left_id, right_id = pair['left_candidate_id'], pair['right_candidate_id']
        if left_id == right_id:
            raise ValueError('similarity_freeze_pair_identity_invalid')
        key = tuple(sorted((left_id, right_id)))
        if key in pair_keys:
            raise ValueError('similarity_freeze_pair_duplicate')
        pair_keys.add(key)
        left, right = candidate_by_id[left_id], candidate_by_id[right_id]
        if pair.get('left_content_hash') != left.get('content_hash') or pair.get('right_content_hash') != right.get('content_hash'):
            raise ValueError('similarity_freeze_pair_binding_invalid')
        status = pair.get('status')
        if status == 'available':
            if type(pair.get('semantic_bps')) is not int or type(pair.get('text_bps')) is not int or type(pair.get('near_duplicate')) is not bool:
                raise ValueError('similarity_freeze_pair_metrics_invalid')
            if pair['near_duplicate'] != (
                pair['semantic_bps'] >= thresholds.semantic_bps and pair['text_bps'] >= thresholds.text_bps
            ):
                raise ValueError('similarity_freeze_pair_decision_invalid')
        elif status == 'exact_content':
            if pair.get('semantic_bps') != 10000 or pair.get('text_bps') != 10000 or pair.get('near_duplicate') is not False:
                raise ValueError('similarity_freeze_exact_pair_invalid')
        elif status != 'unknown_incomplete_evidence' or pair.get('semantic_bps') is not None or pair.get('text_bps') is not None or pair.get('near_duplicate') is not False:
            raise ValueError('similarity_freeze_unknown_pair_invalid')
    expected_pair_keys = {
        (left, right)
        for index, left in enumerate(sorted(ids))
        for right in sorted(ids)[index + 1:]
    }
    if pair_keys != expected_pair_keys:
        raise ValueError('similarity_freeze_pair_coverage_invalid')
    for row in history_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get('order_rank'), int) or row['order_rank'] < 1:
            raise ValueError('similarity_freeze_history_invalid')
        if row.get('status') == 'available' and _question_from_frozen_row(row) is None:
            raise ValueError('similarity_freeze_history_facts_invalid')
        if row.get('status') != 'available' and any(row.get(field) is not None for field in ('normalized_text', 'knowledge_points', 'methods', 'question_type_family')):
            raise ValueError('similarity_freeze_history_facts_invalid')
    usage_ids = set(usage)
    if not usage_ids.issubset(candidate_by_id):
        raise ValueError('similarity_freeze_history_usage_identity_invalid')
    for candidate_id, row in usage.items():
        if not isinstance(row, Mapping) or type(row.get('success_count')) is not int or row['success_count'] < 1 or type(row.get('latest_order_rank')) is not int or row['latest_order_rank'] < 1:
            raise ValueError('similarity_freeze_history_usage_invalid')
    return dict(receipt)


def extend_similarity_freeze(
    receipt: Mapping[str, Any], *, subject: str,
    candidates: Sequence[Mapping[str, Any]], graph_edges: Sequence[Mapping[str, Any]],
    card_facts: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Extend a task freeze after a bounded priority/repair graph expansion.

    Existing candidate facts, pair results, and history are retained byte-for-
    byte in meaning.  Only newly introduced candidates and their pairs are
    evaluated using the already-frozen rule.
    """
    old = validate_similarity_freeze(receipt, graph={'edges': receipt.get('candidate_inputs', ())}, subject=subject)
    # The validator above only needs candidate identity coverage; the actual
    # old graph can be narrower than the new graph, so retain all old rows.
    card_facts = card_facts or {}
    thresholds = SimilarityThresholds(**old['rule']['thresholds'])
    edge_by_id: dict[str, Mapping[str, Any]] = {}
    for edge in graph_edges:
        if isinstance(edge, Mapping):
            edge_by_id.setdefault(str(edge.get('candidate_id')), edge)
    candidate_by_id = {
        str(row.get('candidate_id')): row for row in candidates
        if isinstance(row, Mapping) and row.get('candidate_id') is not None
    }
    old_rows = {str(row['candidate_id']): dict(row) for row in old['candidate_inputs']}
    merged_rows = dict(old_rows)
    for candidate_id, edge in sorted(edge_by_id.items()):
        if candidate_id in merged_rows and (
            merged_rows[candidate_id].get('candidate_fact_hash') == edge.get('candidate_fact_hash')
            and merged_rows[candidate_id].get('content_hash') == edge.get('content_hash')
        ):
            continue
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        text = _snapshot_text(candidate)
        card = _card_for(card_facts, candidate)
        question = None
        if text is not None and card is not None:
            question = prepare_similarity_question(
                content_hash=str(candidate.get('content_hash') or ''), content=text,
                knowledge_points=card.get('knowledge_points', ()), methods=card.get('methods', ()),
                question_type_family=card.get('question_type', candidate.get('question_type_family', '')),
                source_labels=card.get('source_constraints', ()),
            )
            if not question.knowledge_points or not question.methods or not question.question_type_family:
                question = None
        merged_rows[candidate_id] = _identity_row(candidate, edge, question)
    current_rows = [merged_rows[candidate_id] for candidate_id in sorted(edge_by_id) if candidate_id in merged_rows]
    pair_map = {
        tuple(sorted((str(row['left_candidate_id']), str(row['right_candidate_id'])))): dict(row)
        for row in old['pairs']
    }
    for index, left in enumerate(current_rows):
        for right in current_rows[index + 1:]:
            key = tuple(sorted((left['candidate_id'], right['candidate_id'])))
            if key not in pair_map:
                first, second = (left, right) if left['candidate_id'] < right['candidate_id'] else (right, left)
                pair_map[key] = _frozen_pair_row(first, second, thresholds=thresholds)
    history_rows = [dict(row) for row in old['history_inputs']]
    history_questions = [(row, _question_from_frozen_row(row)) for row in history_rows]
    historical_usage = {str(key): dict(value) for key, value in old['historical_near_usage'].items()}
    old_ids = set(old_rows)
    for row in current_rows:
        if row['candidate_id'] in old_ids:
            continue
        question = _question_from_frozen_row(row)
        if question is None:
            continue
        count, latest = 0, 0
        for historical, historical_question in history_questions:
            if historical_question is None or historical.get('content_hash') == row.get('content_hash'):
                continue
            if is_near_duplicate(compare_similarity_questions(question, historical_question), thresholds):
                count += 1
                latest = max(latest, int(historical['order_rank']))
        if count:
            historical_usage[row['candidate_id']] = {'success_count': count, 'latest_order_rank': latest}
    body = {
        'version': old['version'], 'rule': old['rule'], 'subject': old['subject'],
        'candidate_inputs': current_rows,
        'pairs': [pair_map[key] for key in sorted(pair_map) if pair_map[key]['left_candidate_id'] in edge_by_id and pair_map[key]['right_candidate_id'] in edge_by_id],
        'history_inputs': history_rows,
        'historical_near_usage': {key: historical_usage[key] for key in sorted(historical_usage) if key in edge_by_id},
    }
    return {**body, 'freeze_hash': _hash(body)}


def attach_similarity_freeze(graph: Any, receipt: Mapping[str, Any]):
    """Return the same immutable graph type with the receipt embedded."""
    from dataclasses import replace
    payload = graph.to_dict()
    payload['similarity_receipt'] = dict(receipt)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return replace(graph, graph_hash=_hash(payload), _payload_json=raw)


__all__ = [
    'SIMILARITY_VERSION', 'SIMILARITY_FREEZE_VERSION', 'PairSimilarity',
    'SimilarityQuestion', 'SimilarityThresholds', 'calibrated_similarity_rule',
    'prepare_similarity_question', 'compare_similarity_questions',
    'normalize_similarity_text', 'is_near_duplicate',
    'evaluate_similarity_thresholds', 'calibrate_similarity_thresholds',
    'build_similarity_freeze', 'validate_similarity_freeze',
    'extend_similarity_freeze', 'attach_similarity_freeze',
]


def is_near_duplicate(pair: PairSimilarity, thresholds: SimilarityThresholds) -> bool:
    return (
        pair.status == 'available'
        and pair.semantic_bps >= thresholds.semantic_bps
        and pair.text_bps >= thresholds.text_bps
    )


def evaluate_similarity_thresholds(
    samples: Sequence[tuple[PairSimilarity, bool]], thresholds: SimilarityThresholds,
) -> dict[str, int]:
    counts = dict(true_positive=0, false_positive=0, true_negative=0, false_negative=0, unknown=0)
    for pair, positive in samples:
        if pair.status == 'unknown_incomplete_evidence':
            counts['unknown'] += 1
            continue
        predicted = is_near_duplicate(pair, thresholds)
        name = ('true_positive' if predicted else 'false_negative') if positive else (
            'false_positive' if predicted else 'true_negative'
        )
        counts[name] += 1
    return counts


def calibrate_similarity_thresholds(samples: Sequence[tuple[PairSimilarity, bool]]) -> SimilarityThresholds:
    """Fit the approved zero-false-positive rule; callers independently validate."""
    if not samples or not any(label for _, label in samples) or all(label for _, label in samples):
        raise ValueError('similarity_calibration_needs_positive_and_negative_pairs')
    if any(pair.status != 'available' for pair, _ in samples):
        raise ValueError('similarity_calibration_needs_comparable_non_exact_pairs')
    semantic_values = {0, 10001}
    text_values = {0, 10001}
    for pair, _ in samples:
        semantic_values.update((pair.semantic_bps, pair.semantic_bps + 1))
        text_values.update((pair.text_bps, pair.text_bps + 1))
    best = None
    for semantic in sorted(semantic_values):
        for text in sorted(text_values):
            thresholds = SimilarityThresholds(semantic, text)
            counts = evaluate_similarity_thresholds(samples, thresholds)
            if counts['false_positive']:
                continue
            key = (counts['true_positive'], text, semantic)
            if best is None or key > best[0]:
                best = (key, thresholds)
    return best[1]
