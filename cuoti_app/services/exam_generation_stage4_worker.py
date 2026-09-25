"""Strict Stage-4 projection and candidate-fact worker integration.

The semantic source bundle is injected by the durable worker.  This module
never chooses a latest projection, reads an old manifest/checkpoint, or calls a
provider.  The only acceptable authority is the published projection for the
bundle's exact complete snapshot and the currently supported recall versions.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from .exam_generation_stage4_candidate_facts import (
    PROJECTION_IDENTITY_VERSION,
    Stage4CandidateFactsError,
    build_projection_document_hash,
    freeze_candidate_facts,
)
from .exam_generation_stage4_repository import (
    CandidateFactsReceipt,
    Stage4RepositoryError,
    persist_candidate_facts,
    recover_candidate_facts,
)
from .semantic_index_scanner import prepare_semantic_candidates
from .semantic_index_sources import (
    SemanticSourceBundle,
    select_semantic_source_bundle,
)
from .semantic_index import SemanticIndexContractError, semantic_content_hash
from .past_exam_profile_v3 import (
    ProfileV3ContractError,
    semantic_card_question_family,
)
from .semantic_recall_contracts import (
    RECALL_ALGORITHM_VERSION,
    RECALL_DICTIONARY_VERSION,
    RECALL_WEIGHTS_VERSION,
)


@dataclass(frozen=True, slots=True)
class Stage4FactFreezeResult:
    receipt: CandidateFactsReceipt
    projection_identity: Mapping[str, Any]
    candidate_count: int


class Stage4WorkerError(RuntimeError):
    def __init__(
        self,
        code: str,
        reason: str,
        message: str,
        *,
        status_code: int = 409,
        retryable: bool = False,
        category: str = "contract",
        actions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = retryable
        self.category = category
        self.actions = actions


def _fail(
    code: str,
    reason: str,
    message: str,
    *,
    status_code: int = 409,
    retryable: bool = False,
    category: str = "contract",
    actions: tuple[str, ...] = (),
) -> None:
    raise Stage4WorkerError(
        code,
        reason,
        message,
        status_code=status_code,
        retryable=retryable,
        category=category,
        actions=actions,
    )


def _projection_versions(algorithm_version: str) -> tuple[str, str, str]:
    if algorithm_version == RECALL_ALGORITHM_VERSION:
        return (
            RECALL_ALGORITHM_VERSION,
            RECALL_DICTIONARY_VERSION,
            RECALL_WEIGHTS_VERSION,
        )
    if algorithm_version == "semantic-recall-v3":
        return ("semantic-recall-v3", "math-concepts-v2", "semantic-weights-v3")
    _fail(
        "semantic_projection_unavailable",
        "current_projection_algorithm_unsupported",
        "当前语义投影算法版本不受阶段 4 支持",
        actions=("publish_supported_semantic_projection",),
    )


def _projection_identity(
    db: Any, source_snapshot: str, algorithm_version: str, *, source_kinds: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """Load one exact-version authority; chronology is deliberately irrelevant."""

    algorithm, dictionary, weights = _projection_versions(algorithm_version)
    rows = db.execute(
        "SELECT p.id,p.snapshot_id,s.source_snapshot,p.algorithm_version,"
        "p.dictionary_version,p.weights_version,p.build_hash,p.item_count "
        "FROM semantic_recall_projection_versions p "
        "JOIN semantic_index_snapshots s ON s.id=p.snapshot_id "
        "WHERE s.source_snapshot=? AND s.status='complete' "
        "AND p.status='published' AND p.algorithm_version=? "
        "AND p.dictionary_version=? AND p.weights_version=?",
        (
            source_snapshot,
            algorithm,
            dictionary,
            weights,
        ),
    ).fetchall()
    if not rows:
        _fail(
            "semantic_projection_unavailable",
            "current_complete_projection_missing",
            "当前正式语义来源没有对应的已发布完整投影",
            actions=("publish_complete_semantic_projection",),
        )
    if len(rows) > 1 and source_kinds:
        placeholders = ','.join('?' for _ in source_kinds)
        current_count = db.execute(
            'SELECT count(*) FROM semantic_questions q JOIN semantic_card_versions c ON c.id=q.current_card_version_id '
            f'WHERE q.source_kind IN ({placeholders}) AND q.active=1 AND q.excluded=0 '
            "AND c.status='valid' AND c.content_hash=q.content_hash", source_kinds,
        ).fetchone()[0]
        current = []
        for candidate in rows:
            if int(candidate[7]) != current_count:
                continue
            matches = db.execute(
                'SELECT count(*) FROM semantic_recall_documents d JOIN semantic_questions q ON q.id=d.semantic_question_id '
                'JOIN semantic_card_versions c ON c.id=q.current_card_version_id '
                f'WHERE d.projection_id=? AND q.source_kind IN ({placeholders}) '
                'AND d.card_version_id=q.current_card_version_id AND d.content_hash=q.content_hash '
                'AND d.source_kind=q.source_kind AND d.candidate_id=q.candidate_id '
                'AND d.subject=q.subject AND d.chapter=q.chapter AND q.active=1 AND q.excluded=0 '
                "AND c.status='valid' AND c.content_hash=q.content_hash", (candidate[0], *source_kinds),
            ).fetchone()[0]
            if matches == current_count:
                current.append(candidate)
        rows = current
    if len(rows) != 1:
        _fail(
            "semantic_projection_unavailable",
            "current_projection_authority_ambiguous",
            "当前正式语义来源存在多个同版本权威投影",
            actions=("repair_semantic_projection_authority",),
        )
    row = rows[0]
    count = int(
        db.execute(
            "SELECT COUNT(*) FROM semantic_recall_documents WHERE projection_id=?",
            (row[0],),
        ).fetchone()[0]
    )
    if count != int(row[7]):
        _fail(
            "semantic_projection_unavailable",
            "current_projection_document_count_mismatch",
            "当前正式语义投影文档计数不完整",
            actions=("rebuild_semantic_projection",),
        )
    build_hash = str(row[6] or "")
    if len(build_hash) != 64 or any(ch not in "0123456789abcdef" for ch in build_hash):
        _fail(
            "semantic_projection_unavailable",
            "current_projection_build_hash_invalid",
            "当前正式语义投影构建摘要无效",
            actions=("rebuild_semantic_projection",),
        )
    return {
        "identity_version": PROJECTION_IDENTITY_VERSION,
        "projection_id": str(row[0]),
        "semantic_snapshot_id": str(row[1]),
        "source_snapshot": str(row[2]),
        "algorithm_version": str(row[3]),
        "dictionary_version": str(row[4]),
        "weights_version": str(row[5]),
        "projection_build_hash": build_hash,
        "item_count": int(row[7]),
    }


def _source_kinds(candidate_source: object) -> tuple[str, ...]:
    value = str(candidate_source or "").strip()
    if value == "ordinary":
        return ("ordinary",)
    if value == "other_past":
        return ("past_exam_local", "past_exam_other")
    if value == "mixed":
        return ("ordinary", "past_exam_local", "past_exam_other")
    _fail(
        "candidate_fact_unresolved",
        "candidate_source_invalid",
        "阶段 4 候选来源合同无效",
    )


def _candidate_rows(
    db: Any,
    *,
    projection_id: str,
    bundle: Any,
    subject: str,
    source_kinds: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    try:
        prepared, _ = prepare_semantic_candidates(
            bundle.candidates,
            scanned_source_kinds=bundle.scanned_source_kinds,
        )
    except Exception as exc:
        _fail(
            "semantic_projection_unavailable",
            "current_semantic_source_bundle_invalid",
            "当前正式语义来源无法通过完整性校验",
            actions=("repair_semantic_source_bundle",),
        )
    source_by_identity = {
        (str(item.source_kind), str(item.candidate_id)): item
        for item in bundle.candidates
    }
    prepared_by_identity = {item.identity.key: item for item in prepared}
    placeholders = ",".join("?" for _ in source_kinds)
    rows = db.execute(
        "SELECT d.projection_id,d.semantic_question_id,d.card_version_id,"
        "d.source_kind,d.candidate_id,d.subject,d.chapter,d.question_type,"
        "d.difficulty,d.similarity_group,d.source_year,d.source_file_key,"
        "d.confidence,d.content_hash,d.normalized_text_hash,c.card_hash,"
        "c.status,q.current_card_version_id,q.active,q.excluded "
        "FROM semantic_recall_documents d "
        "JOIN semantic_card_versions c ON c.id=d.card_version_id "
        "JOIN semantic_questions q ON q.id=d.semantic_question_id "
        f"WHERE d.projection_id=? AND d.subject=? AND d.source_kind IN ({placeholders}) "
        "ORDER BY d.source_kind,d.candidate_id",
        (projection_id, subject, *source_kinds),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    live: dict[tuple[str, str], dict[str, Any]] = {}
    document_fields = (
        "projection_id", "semantic_question_id", "card_version_id", "source_kind",
        "candidate_id", "subject", "chapter", "question_type", "difficulty",
        "similarity_group", "source_year", "source_file_key", "confidence",
        "content_hash", "normalized_text_hash",
    )
    for row in rows:
        identity = (str(row[3]), str(row[4]))
        source = source_by_identity.get(identity)
        prepared_source = prepared_by_identity.get(identity)
        if source is None or prepared_source is None:
            _fail(
                "candidate_fact_unresolved",
                "projection_candidate_source_snapshot_missing",
                "投影候选未被当前正式来源快照一一覆盖",
            )
        if (
            prepared_source.content_hash != str(row[13])
            or prepared_source.subject != str(row[5])
            or prepared_source.chapter != str(row[6])
            or str(row[16]) != "valid"
            or int(row[17]) != int(row[2])
            or not bool(row[18])
            or bool(row[19])
        ):
            _fail(
                "candidate_fact_unresolved",
                "projection_candidate_source_drift",
                "投影候选与当前正式来源或卡片身份不一致",
            )
        structured_values = (
            source.content,
            *tuple(source.figure_references),
            *tuple(source.options),
            *tuple(source.conditions),
        )
        if (
            not isinstance(source.content, str)
            or not source.content.strip()
            or any(not isinstance(value, str) for value in structured_values)
        ):
            _fail(
                "candidate_fact_unresolved",
                "candidate_content_snapshot_unavailable",
                "候选缺少可重放的结构化内容快照",
            )
        content_snapshot_payload = {
            "schema_version": "semantic-candidate-content-snapshot-v1",
            "content": unicodedata.normalize("NFC", source.content).strip(),
            "figure_references": [
                unicodedata.normalize("NFC", value).strip()
                for value in source.figure_references
            ],
            "options": [
                unicodedata.normalize("NFC", value).strip()
                for value in source.options
            ],
            "conditions": [
                unicodedata.normalize("NFC", value).strip()
                for value in source.conditions
            ],
        }
        try:
            replay_hash = semantic_content_hash(
                content_snapshot_payload["content"],
                figure_references=tuple(content_snapshot_payload["figure_references"]),
                options=tuple(content_snapshot_payload["options"]),
                conditions=tuple(content_snapshot_payload["conditions"]),
            )
        except SemanticIndexContractError:
            replay_hash = ""
        if replay_hash != str(row[13]):
            _fail(
                "candidate_fact_unresolved",
                "candidate_content_snapshot_hash_mismatch",
                "候选结构化内容快照无法重放投影内容身份",
            )
        document = dict(zip(document_fields, tuple(row)[:15]))
        projection_document_hash = build_projection_document_hash(document)
        item: dict[str, Any] = {
            "source_kind": identity[0],
            "candidate_id": identity[1],
            "subject": str(row[5]),
            "question_type_family": str(row[7]),
            "content_hash": str(row[13]),
            "content_snapshot": json.dumps(
                content_snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "card_version_id": int(row[2]),
            "card_hash": str(row[15]),
            "projection_document_hash": projection_document_hash,
            "difficulty": row[8],
            "difficulty_confidence": row[12],
        }
        if identity[0] == "ordinary":
            item["chapter"] = str(row[6])
        else:
            # source_note/path/ID prefixes are not provenance identities.  A
            # source bundle that cannot provide these explicit immutable
            # fields must be rebuilt with the Stage-4 provenance projection.
            source_exam_id = str(getattr(source, "source_exam_id", "") or "").strip()
            section_id = str(getattr(source, "section_id", "") or "").strip()
            institution = unicodedata.normalize(
                "NFC",
                str(getattr(source, "source_institution", "") or ""),
            ).strip()
            if not source_exam_id or not section_id or not institution:
                _fail(
                    "semantic_projection_unavailable",
                    "past_exam_other_provenance_projection_missing",
                    "当前正式语义投影缺少外校真题的试卷、分区或院校身份",
                    actions=("rebuild_semantic_projection_with_provenance",),
                )
            item.update(
                {
                    "source_exam_id": source_exam_id,
                    "section_id": section_id,
                    "source_institution": institution,
                }
            )
        # Use the same card-label mapping as profile construction. Unknown
        # source labels remain ineligible; original card types stay intact.
        try:
            item["question_type_family"] = semantic_card_question_family(
                item["question_type_family"]
            )
        except ProfileV3ContractError:
            continue
        candidates.append(item)
        live[identity] = {
            "content_hash": str(row[13]),
            "card_version_id": int(row[2]),
            "card_hash": str(row[15]),
            "projection_document_hash": projection_document_hash,
        }
    if not candidates:
        _fail(
            "candidate_fact_unresolved",
            "current_projection_candidate_scope_empty",
            "当前正式语义投影没有覆盖请求科目与候选来源",
            actions=("publish_matching_semantic_projection",),
        )
    return candidates, live


def select_other_institution_candidates(candidates, live, *, target_institution):
    """Apply the target-relative source exclusion before freezing hard facts."""
    target = unicodedata.normalize("NFC", target_institution).strip().casefold()
    selected = [
        item for item in candidates
        if item["source_kind"] == "ordinary"
        or unicodedata.normalize("NFC", item["source_institution"]).strip().casefold() != target
    ]
    selected_live = {
        (item["source_kind"], item["candidate_id"]): live[(item["source_kind"], item["candidate_id"])]
        for item in selected
    }
    return selected, selected_live


def freeze_and_persist_stage4_candidate_facts(
    db: Any,
    *,
    job_id: str,
    payload: Mapping[str, Any],
    profile_context: Any,
    source_bundle_loader: Callable[[], Any],
    projection_algorithm_version: str,
    worker_id: str,
    lease_attempt: int,
) -> Stage4FactFreezeResult:
    """Freeze current projection facts and persist them behind attempt fences."""

    try:
        recovered = recover_candidate_facts(
            db,
            job_id=job_id,
            worker_id=worker_id,
            lease_attempt=lease_attempt,
        )
    except Stage4RepositoryError as exc:
        _fail(
            exc.code,
            exc.reason,
            str(exc),
            status_code=503 if exc.retryable else 409,
            retryable=exc.retryable,
            category="storage" if exc.retryable else "contract",
        )
    if recovered is not None:
        row = db.execute(
            "SELECT facts_payload_json FROM exam_generation_stage4_fact_receipts WHERE job_id=?",
            (job_id,),
        ).fetchone()
        payload_value = json.loads(str(row[0]))
        return Stage4FactFreezeResult(
            recovered,
            dict(payload_value["projection_identity"]),
            recovered.candidate_count,
        )
    try:
        source_bundle = source_bundle_loader()
    except Exception as exc:
        _fail(
            "semantic_projection_unavailable",
            str(getattr(exc, "code", "current_semantic_source_bundle_unavailable") or "current_semantic_source_bundle_unavailable").casefold(),
            "当前正式语义来源不可用",
            status_code=503,
            retryable=True,
            category="dependency",
            actions=("repair_semantic_source_bundle",),
        )
    requested_source_kinds = _source_kinds(payload.get("candidate_source"))
    if isinstance(source_bundle, SemanticSourceBundle):
        try:
            source_bundle = select_semantic_source_bundle(
                source_bundle,
                requested_source_kinds,
            )
        except Exception as exc:
            _fail(
                "semantic_projection_unavailable",
                str(getattr(exc, "code", "release_source_scope_invalid") or "release_source_scope_invalid").casefold(),
                "当前正式语义来源无法形成请求范围的完整快照",
                actions=("publish_matching_semantic_projection",),
            )
    source_snapshot = str(getattr(source_bundle, "source_snapshot", "") or "").strip()
    if not source_snapshot:
        _fail(
            "semantic_projection_unavailable",
            "current_semantic_source_snapshot_missing",
            "当前正式语义来源缺少完整快照身份",
        )
    first_projection = _projection_identity(
        db, source_snapshot, projection_algorithm_version, source_kinds=requested_source_kinds
    )
    try:
        profile_payload = profile_context.to_dict()
        combination = profile_payload["profile_binding"]["combination"]
        subject = unicodedata.normalize("NFC", str(combination["subject"])).strip()
        target = unicodedata.normalize("NFC", str(combination["institution"])).strip()
    except (AttributeError, TypeError, ValueError, KeyError):
        _fail(
            "candidate_fact_unresolved",
            "stage4_profile_combination_missing",
            "阶段 4 缺少已绑定的院校与科目身份",
        )
    if target != unicodedata.normalize("NFC", str(payload.get("institution") or "")).strip():
        _fail(
            "candidate_fact_unresolved",
            "stage4_target_institution_drift",
            "阶段 4 目标院校与已绑定画像不一致",
        )
    candidates, live = _candidate_rows(
        db,
        projection_id=first_projection["projection_id"],
        bundle=source_bundle,
        subject=subject,
        source_kinds=requested_source_kinds,
    )
    candidates, live = select_other_institution_candidates(
        candidates, live, target_institution=target,
    )
    second_projection = _projection_identity(
        db, source_snapshot, projection_algorithm_version, source_kinds=requested_source_kinds
    )
    try:
        frozen = freeze_candidate_facts(
            projection=first_projection,
            expected_projection=second_projection,
            candidates=candidates,
            current_candidate_identities=live,
            target_institution=target,
        )
        receipt = persist_candidate_facts(
            db,
            job_id=job_id,
            frozen=frozen,
            worker_id=worker_id,
            lease_attempt=lease_attempt,
        )
    except Stage4CandidateFactsError as exc:
        _fail(exc.code, exc.reason, str(exc))
    except Stage4RepositoryError as exc:
        _fail(
            exc.code,
            exc.reason,
            str(exc),
            status_code=503 if exc.retryable else 409,
            retryable=exc.retryable,
            category="storage" if exc.retryable else "contract",
        )
    return Stage4FactFreezeResult(receipt, first_projection, len(candidates))


__all__ = [
    "Stage4FactFreezeResult",
    "Stage4WorkerError",
    "freeze_and_persist_stage4_candidate_facts",
]
