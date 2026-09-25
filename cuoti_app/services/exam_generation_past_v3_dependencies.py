"""Production adapters for the strict formal-past v3 coordinator.

The module wires only already-frozen repositories and explicitly supplied
authorities.  It deliberately does not inspect Flask configuration or create
provider/model defaults.  A missing authority remains ``None`` so the
coordinator returns its stable fail-closed 422 result before any callback can
be invoked.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .exam_generation_past_v3_pipeline import PipelineDependencies
from .exam_generation_phase8_authority import (
    build_stage5_policy_fact_set,
    build_stage7_global_constraint_evidence,
    load_completed_difficulty_results,
)
from .exam_generation_profile_worker import PastFrozenProfileContext
from .exam_generation_stage3_contract import persist_stage3_generation_contract
from .exam_generation_stage4_candidate_facts import FrozenCandidateFacts
from .exam_generation_stage4_repository import (
    Stage4RepositoryError,
    recover_candidate_facts,
)
from .exam_generation_stage5_repository import (
    Stage5RepositoryError,
    recover_stage5_state,
)
from .exam_generation_stage5_worker import run_stage5_selection_worker
from .exam_generation_stage6_repository import (
    Stage6RepositoryError,
    recover_stage6_state,
)
from .exam_generation_stage6_worker import run_stage6_scoring_worker
from .exam_generation_stage7_quality import Stage7Budget
from .exam_generation_stage7_repository import (
    Stage7RepositoryError,
    recover_stage7_state,
)
from .exam_generation_stage7_worker import run_stage7_worker
from .exam_generation_v3_publish import (
    NativeV3PublishOutcome,
    NativeV3PaperDraft,
    assemble_v3_single_paper,
    publish_v3_single_paper,
    recover_v3_publication_receipt,
)


AuthorityLoader = Callable[..., Mapping[str, Any]]
ModelCallback = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PastV3ProductionAuthorities:
    """Every non-repository authority needed by one formal-past attempt.

    Values are intentionally explicit and job-independent.  In particular,
    ``score_model_id`` and the prompt/provider bindings are never read from
    application configuration by this module.
    """

    profile_context: PastFrozenProfileContext | None
    authority_registry_loader: AuthorityLoader | None
    recent_usage_snapshot_loader: AuthorityLoader | None
    score_callback: ModelCallback | None
    score_prompt_version: str | None
    score_model_id: str | None
    quality_callback: ModelCallback | None
    subject_id: int | None
    title: str | None
    artifact_dir: str | Path | None
    artifact_callback: ModelCallback | None
    classification_binding: Mapping[str, Any] | None = None
    verification_binding: Mapping[str, Any] | None = None
    classify_callback: ModelCallback | None = None
    difficulty_verify_callback: ModelCallback | None = None
    score_provider_capability: Any = None
    score_transport_retry_limit: int = 2
    adaptation_callback: ModelCallback | None = None
    verification_callback: ModelCallback | None = None
    receipt_callback: ModelCallback | None = None
    usage_loader: ModelCallback | None = None
    stage7_budget: Stage7Budget = Stage7Budget()
    # New protocol fields are explicit and opt-in.  The default keeps legacy
    # published-task recovery readable while preventing accidental migration.
    scoring_protocol: str = "legacy_score_read_only_v2"
    rubric_callback: ModelCallback | None = None
    rubric_prompt_version: str | None = None
    rubric_model_id: str | None = None
    utility_policy: Mapping[str, Any] | None = None
    priority_once: bool = False
    memory: Any = None
    pending_memory_request: Mapping[str, Any] | None = None
    memory_subject: str | None = None
    memory_logger: Any = None
    render_probe: Any = None


def _candidate_facts(
    db: Any, *, job_id: str, worker_id: str, lease_attempt: int
) -> FrozenCandidateFacts:
    recovered = recover_candidate_facts(
        db,
        job_id=job_id,
        worker_id=worker_id,
        lease_attempt=lease_attempt,
    )
    if recovered is None:
        raise Stage4RepositoryError(
            "exam_stage4_incomplete", "candidate_facts_receipt_missing",
            "阶段 4 冻结候选事实回执不存在",
        )
    row = db.execute(
        "SELECT candidate_facts_contract_version,projection_identity_hash,"
        "candidate_facts_hash,target_institution,facts_payload_json,"
        "receipt_hash,candidate_count,unresolved_count "
        "FROM exam_generation_stage4_fact_receipts WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise Stage4RepositoryError(
            "exam_stage4_hash_mismatch", "candidate_facts_receipt_disappeared",
            "阶段 4 冻结候选事实回执在恢复后消失",
        )
    try:
        payload = json.loads(str(row["facts_payload_json"]))
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        candidate_count = len(payload.get("candidates", ()))
        unresolved_count = len(payload.get("unresolved_candidate_ids", ()))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Stage4RepositoryError(
            "exam_stage4_hash_mismatch", "candidate_facts_payload_invalid",
            "阶段 4 冻结候选事实载荷无效",
        ) from exc
    if (
        recovered.job_id != job_id
        or recovered.candidate_facts_hash != str(row["candidate_facts_hash"])
        or recovered.candidate_facts_hash != payload_hash
        or recovered.receipt_hash != str(row["receipt_hash"])
        or recovered.candidate_count != int(row["candidate_count"])
        or recovered.unresolved_count != int(row["unresolved_count"])
        or recovered.candidate_count != candidate_count
        or recovered.unresolved_count != unresolved_count
    ):
        raise Stage4RepositoryError(
            "exam_stage4_hash_mismatch", "candidate_facts_recovery_binding_drift",
            "阶段 4 恢复回执、数据库行与冻结载荷未闭合",
        )
    return FrozenCandidateFacts(
        str(row["candidate_facts_contract_version"]),
        str(row["projection_identity_hash"]),
        str(row["candidate_facts_hash"]),
        str(row["target_institution"]),
        tuple(payload.get("candidates", ())),
        tuple(payload.get("unresolved_candidate_ids", ())),
        payload_json,
    )


def build_past_v3_production_dependencies(
    authorities: PastV3ProductionAuthorities,
) -> PipelineDependencies:
    """Build coordinator dependencies without consulting runtime AI config."""

    if not isinstance(authorities, PastV3ProductionAuthorities):
        raise TypeError("authorities must be PastV3ProductionAuthorities")

    profile_context = authorities.profile_context

    def stage3_receipt(db: Any, **actor: Any):
        if profile_context is None:
            raise RuntimeError("past_v3_profile_context_missing")
        return persist_stage3_generation_contract(
            db, profile_context=profile_context, **actor
        )

    def stage3_loader(*, job_id: str, stage3_receipt: Mapping[str, Any]):
        del stage3_receipt
        # The worker loader has no database argument, so the immutable contract
        # is captured only after the adapter has recovered it for this job.
        receipt = recovered_contracts.get(job_id)
        if receipt is None:
            raise RuntimeError("past_v3_stage3_contract_not_recovered")
        return receipt.contract.to_dict()

    def stage5_runner(db: Any, **kwargs: Any):
        receipt = stage3_receipt(
            db,
            job_id=kwargs["job_id"],
            worker_id=kwargs["worker_id"],
            lease_attempt=kwargs["lease_attempt"],
        )
        recovered_contracts[kwargs["job_id"]] = receipt
        return run_stage5_selection_worker(db, **kwargs)

    recovered_contracts: dict[str, Any] = {}

    def policy_loader(**kwargs: Any):
        registry_loader = authorities.authority_registry_loader
        recent_loader = authorities.recent_usage_snapshot_loader
        if registry_loader is None or recent_loader is None:
            raise RuntimeError("past_v3_policy_authority_missing")
        registry = registry_loader(**kwargs)
        recent = recent_loader(**kwargs)
        return build_stage5_policy_fact_set(
            stage3=kwargs["stage3"],
            stage4=kwargs["stage4"],
            authority_registry=registry,
            recent_usage_snapshot=recent,
        )

    def publication_recovery(db: Any, **actor: Any):
        head = db.execute(
            "SELECT state FROM exam_generation_v3_publish_heads WHERE job_id=?",
            (actor["job_id"],),
        ).fetchone()
        if head is None or str(head["state"] or "") == "request_frozen":
            return None
        if (
            authorities.scoring_protocol == "legacy_score_read_only_v2"
            and str(head["state"] or "") == "published_pending_receipt"
        ):
            # Legacy recovery is now strictly read-only.  Report the already
            # committed publication without completing or writing its missing
            # receipt; the coordinator returns the stable pending state.
            row = db.execute(
                "SELECT h.request_hash,h.publication_hash,p.exam_id "
                "FROM exam_generation_v3_publish_heads h "
                "JOIN exam_generation_v3_publications p ON p.job_id=h.job_id "
                "WHERE h.job_id=? AND h.state='published_pending_receipt'",
                (actor["job_id"],),
            ).fetchone()
            if row is None:
                return None
            return NativeV3PublishOutcome(
                str(actor["job_id"]),
                "published_pending_receipt",
                str(row["request_hash"]),
                int(row["exam_id"]),
                str(row["publication_hash"]),
                None,
                True,
            ).to_coordinator_result()
        return recover_v3_publication_receipt(
            db, receipt_callback=authorities.receipt_callback, **actor
        ).to_coordinator_result()

    def stage7_head_loader(db: Any, **actor: Any):
        row = db.execute(
            "SELECT 1 FROM exam_generation_stage7_heads WHERE job_id=?",
            (actor["job_id"],),
        ).fetchone()
        return None if row is None else recover_stage7_state(db, **actor)

    def stage6_state_loader(db: Any, **actor: Any):
        row = db.execute(
            "SELECT 1 FROM exam_generation_stage6_heads WHERE job_id=?",
            (actor["job_id"],),
        ).fetchone()
        if row is None:
            return None
        try:
            state = recover_stage6_state(db, **actor)
        except Stage6RepositoryError as exc:
            if exc.reason in {"closed_state_incomplete", "stage6_head_missing"}:
                return {"status": "started"}
            raise
        return {
            "status": "stage6_scored",
            "checkpoint_hash": state.head.checkpoint_hash,
            "score_set_hash": state.head.score_set_hash,
            "selection_hash": state.head.selection_hash,
        }

    def stage5_authority(db: Any, actor: Mapping[str, Any]):
        state = recover_stage5_state(db, **actor)
        if state.selection is None or state.active_graph is None:
            raise Stage5RepositoryError(
                "exam_stage5_incomplete", "selection_missing",
                "阶段 5 选择尚未完成",
            )
        facts = _candidate_facts(
            db,
            job_id=str(actor["job_id"]),
            worker_id=str(actor["worker_id"]),
            lease_attempt=int(actor["lease_attempt"]),
        )
        score = recover_stage6_state(db, **actor).score_receipt
        return state, facts, score

    def difficulty_loader(db: Any, **actor: Any):
        state, facts, _score = stage5_authority(db, actor)
        return load_completed_difficulty_results(
            db, selection=state.selection, candidate_facts=facts, **actor
        )

    def global_evidence_loader(db: Any, **actor: Any):
        state, _facts, score = stage5_authority(db, actor)
        return build_stage7_global_constraint_evidence(
            selection=state.selection,
            constraint=state.contract,
            active_graph=state.active_graph,
            score_receipt=score,
        )

    def assembler(db: Any, *, stage7_result: Any, **actor: Any):
        del stage7_result
        state = recover_stage7_state(db, **actor)
        receipt = stage3_receipt(db, **actor)
        if state.result is None or state.checkpoint is None:
            raise Stage7RepositoryError(
                "exam_stage7_incomplete", "completed_state_incomplete",
                "阶段 7 尚未形成最终结果与 checkpoint",
            )
        draft = assemble_v3_single_paper(
            stage7_checkpoint=state.checkpoint,
            stage7_result=state.result,
            stage3_contract=receipt.contract,
            title=str(authorities.title or ""),
        )
        return {
            "draft_hash": draft.draft_hash,
            "draft": draft,
            "stage7_result": state.result,
            "stage7_checkpoint": state.checkpoint,
            "stage3_contract": receipt.contract,
        }

    def native_publisher(db: Any, *, exam: Mapping[str, Any], **actor: Any):
        draft = exam.get("draft")
        if not isinstance(draft, NativeV3PaperDraft) or exam.get("draft_hash") != draft.draft_hash:
            raise TypeError("assembled native draft is invalid")
        outcome = publish_v3_single_paper(
            db,
            subject_id=int(authorities.subject_id),
            title=str(authorities.title),
            stage7_checkpoint=exam["stage7_checkpoint"],
            stage7_result=exam["stage7_result"],
            stage3_contract=exam["stage3_contract"],
            artifact_dir=str(authorities.artifact_dir),
            artifact_callback=authorities.artifact_callback,
            receipt_callback=authorities.receipt_callback,
            **actor,
        )
        return outcome.to_coordinator_result()

    policy_ready = (
        callable(authorities.authority_registry_loader)
        and callable(authorities.recent_usage_snapshot_loader)
    )
    publish_ready = (
        type(authorities.subject_id) is int
        and authorities.subject_id > 0
        and isinstance(authorities.title, str)
        and bool(authorities.title.strip())
        and isinstance(authorities.artifact_dir, (str, Path))
        and callable(authorities.artifact_callback)
    )

    def d2_runner(db: Any, *, lifecycle_guard=None, **actor: Any):
        from .exam_generation_d2_runtime import run_d2_generation

        receipt = stage3_receipt(db, **actor)
        recovered_contracts[actor["job_id"]] = receipt
        return run_d2_generation(
            db, stage3_contract=receipt.contract,
            facts_loader=lambda: _candidate_facts(db, **actor),
            stage5_runner=stage5_runner, stage3_contract_loader=stage3_loader,
            policy_fact_loader=policy_loader,
            classification_binding=authorities.classification_binding,
            verification_binding=authorities.verification_binding,
            classify_callback=authorities.classify_callback,
            difficulty_verify_callback=authorities.difficulty_verify_callback,
            quality_callback=authorities.quality_callback,
            subject_id=authorities.subject_id, title=authorities.title,
            artifact_dir=authorities.artifact_dir,
            artifact_callback=authorities.artifact_callback,
            receipt_callback=authorities.receipt_callback,
            priority_once=authorities.priority_once,
            priority_reselection_limit=max(
                0, authorities.stage7_budget.structural_calls_remaining - 1
            ),
            lifecycle_guard=lifecycle_guard, **actor,
            memory=authorities.memory,
            pending_memory_request=authorities.pending_memory_request,
            memory_subject=authorities.memory_subject,
            memory_logger=authorities.memory_logger,
            render_probe=authorities.render_probe,
        )

    return PipelineDependencies(
        publication_recovery=publication_recovery,
        stage7_head_loader=stage7_head_loader,
        stage6_state_loader=stage6_state_loader,
        stage5_runner=stage5_runner,
        stage6_runner=run_stage6_scoring_worker,
        stage7_runner=run_stage7_worker,
        assembler=assembler if publish_ready else None,
        native_publisher=native_publisher if publish_ready else None,
        stage3_contract_loader=stage3_loader if profile_context is not None else None,
        policy_fact_loader=policy_loader if policy_ready else None,
        score_receipt_loader=None,
        classification_binding=authorities.classification_binding,
        verification_binding=authorities.verification_binding,
        classify_callback=authorities.classify_callback,
        difficulty_verify_callback=authorities.difficulty_verify_callback,
        score_callback=authorities.score_callback,
        score_prompt_version=authorities.score_prompt_version,
        score_model_id=authorities.score_model_id,
        stage7_difficulty_loader=difficulty_loader,
        stage7_global_evidence_loader=global_evidence_loader,
        quality_callback=authorities.quality_callback,
        adaptation_callback=authorities.adaptation_callback,
        verification_callback=authorities.verification_callback,
        stage7_budget=authorities.stage7_budget,
        score_provider_capability=authorities.score_provider_capability,
        score_transport_retry_limit=authorities.score_transport_retry_limit,
        usage_loader=authorities.usage_loader,
        scoring_protocol=authorities.scoring_protocol,
        rubric_callback=authorities.rubric_callback,
        rubric_prompt_version=authorities.rubric_prompt_version,
        rubric_model_id=authorities.rubric_model_id,
        utility_policy=authorities.utility_policy,
        d2_runner=d2_runner if policy_ready and publish_ready else None,
    )


def make_past_v3_production_dependencies_factory(
    authorities_loader: Callable[..., PastV3ProductionAuthorities],
) -> Callable[..., PipelineDependencies]:
    """Return the callable registered under the integration extension name."""

    if not callable(authorities_loader):
        raise TypeError("authorities_loader must be callable")

    def factory(**context: Any) -> PipelineDependencies:
        authorities = authorities_loader(**context)
        return build_past_v3_production_dependencies(authorities)

    return factory


__all__ = [
    "PastV3ProductionAuthorities",
    "build_past_v3_production_dependencies",
    "make_past_v3_production_dependencies_factory",
]
