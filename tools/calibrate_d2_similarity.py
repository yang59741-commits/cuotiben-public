"""Propose read-only source pairs and evaluate explicitly reviewed labels."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuoti_app.services.exam_generation_similarity import (
    SIMILARITY_VERSION, SimilarityThresholds, calibrate_similarity_thresholds,
    compare_similarity_questions, evaluate_similarity_thresholds, is_near_duplicate,
    prepare_similarity_question,
)
from cuoti_app.services.semantic_index import semantic_content_hash


def readonly(path):
    db = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def propose(database, bank, artifact):
    with readonly(bank) as db:
        bodies = {('ordinary', r['question_id']): r['content']
                  for r in db.execute('SELECT question_id,content FROM questions')}
    document = json.loads(Path(artifact).read_text(encoding='utf-8'))
    bodies.update({(r['source_kind'], r['candidate_id']): r['content'] for r in document['candidates']})
    with readonly(database) as db:
        rows = db.execute(
            "SELECT q.source_kind,q.candidate_id,q.content_hash,q.subject,c.id AS card_version_id,c.card_json "
            "FROM semantic_questions q JOIN semantic_card_versions c ON c.id=q.current_card_version_id "
            "WHERE q.active=1 AND q.excluded=0 AND c.status='valid' AND c.content_hash=q.content_hash"
        ).fetchall()
    buckets = defaultdict(list)
    questions = {}
    for row in rows:
        content = bodies.get((row['source_kind'], row['candidate_id']))
        if not content or not 35 <= len(content) <= 520:
            continue
        card = json.loads(row['card_json'])
        prepared = prepare_similarity_question(
            content_hash=row['content_hash'], content=content,
            knowledge_points=card['knowledge_points'], methods=card['methods'],
            question_type_family=card['question_type'],
            source_labels=card.get('source_constraints', ()),
        )
        if not prepared.knowledge_points or not prepared.methods:
            continue
        key = (row['subject'], tuple(sorted(prepared.knowledge_points)), prepared.question_type_family)
        identity = (row['source_kind'], row['candidate_id'])
        questions[identity] = (prepared, dict(row) | {'content': content, 'card': card})
        buckets[key].append(identity)
    compared = set()
    pairs = defaultdict(list)
    for bucket, identities in sorted(buckets.items()):
        identities.sort(key=lambda identity: questions[identity][0].text)
        for i, left_id in enumerate(identities):
            neighbours = identities[i + 1:i + 5]
            if len(identities) > 8:
                neighbours += [identities[(i + len(identities) // 2) % len(identities)]]
            for right_id in neighbours:
                key = tuple(sorted((left_id, right_id)))
                if left_id == right_id or key in compared:
                    continue
                compared.add(key)
                left, right = questions[left_id], questions[right_id]
                pair = compare_similarity_questions(left[0], right[0])
                if pair.status != 'available':
                    continue
                pairs[bucket[0]].append((pair, bucket, left[1], right[1]))
    output = []
    for subject, candidates in sorted(pairs.items()):
        candidates.sort(key=lambda r: (-r[0].text_bps, -r[0].semantic_bps,
                                       r[2]['candidate_id'], r[3]['candidate_id']))
        used = set()
        for category, ordering in (('similar_proposal', candidates), ('contrast_proposal', list(reversed(candidates)))):
            per_bucket = defaultdict(int)
            count = 0
            for pair, bucket, left, right in ordering:
                ids = {left['content_hash'], right['content_hash']}
                if used & ids or per_bucket[bucket] >= 2:
                    continue
                if any(semantic_content_hash(r['content']) != r['content_hash'] for r in (left, right)):
                    continue
                def public(row):
                    return {k: row[k] for k in ('source_kind', 'candidate_id', 'content_hash',
                                                'card_version_id', 'content', 'card')}
                output.append({'pair_id': f'pair-{len(output) + 1:03}', 'subject': subject,
                               'proposal': category, 'comparison': asdict(pair),
                               'left': public(left), 'right': public(right)})
                used.update(ids)
                per_bucket[bucket] += 1
                count += 1
                if count == 20:
                    break
    return {'version': SIMILARITY_VERSION, 'stage': 'unreviewed_proposals',
            'provider_calls': 0, 'database_writes': 0, 'pairs': output}


def evaluate(pool, labels):
    by_id = {row['pair_id']: row for row in pool['pairs']}
    groups = defaultdict(lambda: defaultdict(list))
    identities = defaultdict(lambda: defaultdict(set))
    seen = set()
    reviewed = []
    for label in labels:
        pair_id = label['pair_id']
        if pair_id in seen:
            raise ValueError('duplicate_reviewed_pair')
        seen.add(pair_id)
        row = by_id[pair_id]
        split = label['split']
        if split not in {'calibration', 'validation'} or type(label['near_duplicate']) is not bool:
            raise ValueError('invalid_review_label')
        prepared = [prepare_similarity_question(
            content_hash=side['content_hash'], content=side['content'],
            knowledge_points=side['card']['knowledge_points'], methods=side['card']['methods'],
            question_type_family=side['card']['question_type'],
            source_labels=side['card'].get('source_constraints', ()),
        ) for side in (row['left'], row['right'])]
        comparison = compare_similarity_questions(*prepared)
        reviewed.append((row, label, comparison))
        groups[row['subject']][split].append((comparison, label['near_duplicate']))
        identities[row['subject']][split].update((row['left']['content_hash'], row['right']['content_hash']))
    results = {}
    for subject, splits in groups.items():
        if identities[subject]['calibration'] & identities[subject]['validation']:
            raise ValueError('calibration_validation_question_overlap')
        fit, validation = splits['calibration'], splits['validation']
        if len(fit) != 16 or len(validation) != 8 or sum(label for _, label in fit) != 8 or sum(label for _, label in validation) != 4:
            raise ValueError('expected_balanced_16_calibration_8_validation')
        thresholds = calibrate_similarity_thresholds(fit)
        counts = evaluate_similarity_thresholds(validation, thresholds)
        results[subject] = {'thresholds': asdict(thresholds),
                            'calibration': evaluate_similarity_thresholds(fit, thresholds),
                            'validation': counts,
                            'accepted': counts['false_positive'] == 0 and counts['true_positive'] >= 3 and counts['unknown'] == 0}
    # Keep the actual reviewed inputs and outcomes with the calibration receipt.
    # Proposal direction is intentionally not used as a ground-truth label.
    evidence = []
    for row, label, comparison in reviewed:
        thresholds = SimilarityThresholds(**results[row['subject']]['thresholds'])
        evidence.append({
            **label, 'subject': row['subject'], 'comparison': asdict(comparison),
            'predicted_near_duplicate': is_near_duplicate(comparison, thresholds),
            'left': row['left'], 'right': row['right'],
        })
    return {'version': SIMILARITY_VERSION, 'subjects': results,
            'reviewed_pairs': evidence, 'provider_calls': 0, 'database_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    propose_args = sub.add_parser('propose')
    propose_args.add_argument('--database', type=Path, default=ROOT / 'data/cuoti.db')
    propose_args.add_argument('--bank', type=Path, default=ROOT / 'instance/question_bank.sqlite3')
    propose_args.add_argument('--artifact', type=Path, default=ROOT / 'instance/semantic_index/formal_past_exams.json')
    propose_args.add_argument('--output', type=Path, required=True)
    check = sub.add_parser('evaluate')
    check.add_argument('--pool', type=Path, required=True)
    check.add_argument('--labels', type=Path, required=True)
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'propose':
        result = propose(args.database, args.bank, args.artifact)
        summary = {'proposed_pairs': len(result['pairs']), 'provider_calls': 0}
    else:
        result = evaluate(json.loads(args.pool.read_text(encoding='utf-8')),
                          json.loads(args.labels.read_text(encoding='utf-8')))
        summary = {key: value for key, value in result.items() if key != 'reviewed_pairs'}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
