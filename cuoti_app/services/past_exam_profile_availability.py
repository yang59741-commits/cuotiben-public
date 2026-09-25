"""Read-only public projection of persisted active exact-slot profile v3."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .exam_query import subject_ids_by_name
from .past_exam_profile_activation import (
    normalize_profile_combination,
    profile_availability,
    resolve_selectable_profile,
)
from .past_exam_profile_activation_store import (
    ProfileActivationStoreError,
    load_activation_state_from_database,
)
from .past_exam_profile_v3 import ProfileV3ContractError


PROFILE_AVAILABILITY_SCHEMA_VERSION = 1
PROFILE_AVAILABILITY_CONTRACT_VERSION = (
    "past-exam-profile-availability-v1"
)
D2_SOURCE_MODES = ("ordinary", "other_past", "mixed")
# A coverage-only terminal decision for targets that cannot form a verifiable
# exact-slot profile without obtaining a new real source paper.  It never
# changes candidate eligibility; it only explains why that target stays
# unavailable while its existing valid cards remain in the shared pool.
PROFILE_ESTABLISHMENT_ABANDONED = "abandoned"
PROFILE_ABANDONED_CODE = "profile_abandoned"
_PROFILE_ABANDONED_PRIORITY_INSTITUTIONS = frozenset(
    {"湖南师范大学", "大连理工大学"}
)
_PROFILE_ABANDONED_MESSAGE = (
    "画像建设已停止（未取得可用于补齐的真实源题）；"
    "现有有效题目仍保留为通用候选。"
)
# The persisted worker freeze and the v3-native downstream generation stages
# are both complete.  The capability only opens exact active-v3 combinations;
# legacy profiles and inactive combinations remain fail-closed.
GENERATION_SUBMISSION_SUPPORTED = True

_EMPTY_MESSAGE = (
    "当前没有已确认并显式激活的真题画像 v3；"
    "旧画像、草稿和未激活组合不能用于正式组卷。"
)


def load_d2_coverage(path: Path | None) -> dict[str, Any] | None:
    """Read the one lightweight local D2 coverage table when it is present.

    Coverage is an informational projection produced by the read-only
    preflight tool.  A missing or stale file must not make an otherwise
    supported profile disappear from the UI; the persisted profile gate stays
    authoritative in that case.
    """
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) and isinstance(value.get("targets"), list) else None


def _coverage_target(coverage: Mapping[str, Any] | None, *, institution: str,
                     subject: str, reference_year: int) -> Mapping[str, Any] | None:
    if not isinstance(coverage, Mapping):
        return None
    for target in coverage.get("targets", ()):
        if not isinstance(target, Mapping):
            continue
        # An abandoned target can retain the parser's latest year while its
        # proposed profile year points at an older incomplete fallback.  Keep
        # that explicit disposition tied to the year it actually describes;
        # do not leak it onto unrelated year structures.
        if (
            (
                target.get("profile_status") == PROFILE_ESTABLISHMENT_ABANDONED
                or target.get("profile_establishment") == PROFILE_ESTABLISHMENT_ABANDONED
            )
            and target.get("abandonment_reference_year") is not None
        ):
            if (
                target.get("institution") == institution
                and target.get("subject") == subject
                and target.get("abandonment_reference_year") == reference_year
            ):
                return target
            continue
        active_year = target.get("active_reference_year")
        proposed_year = target.get("proposed_reference_year")
        if (target.get("institution") == institution and target.get("subject") == subject
                and (active_year if active_year is not None else proposed_year) == reference_year):
            return target
    return None


def _coverage_profile_abandonment(
    coverage: Mapping[str, Any] | None,
    *,
    institution: str,
    subject: str,
    reference_year: int,
) -> dict[str, Any] | None:
    """Return an explicit coverage decision without becoming a profile gate.

    The two real-acceptance institutions are deliberately excluded here.  A
    stale or hand-edited coverage record therefore cannot hide either
    priority profile once its persisted activation exists.
    """
    if institution in _PROFILE_ABANDONED_PRIORITY_INSTITUTIONS:
        return None
    target = _coverage_target(
        coverage,
        institution=institution,
        subject=subject,
        reference_year=reference_year,
    )
    if not isinstance(target, Mapping):
        return None
    if not (
        target.get("profile_status") == PROFILE_ESTABLISHMENT_ABANDONED
        or target.get("profile_establishment") == PROFILE_ESTABLISHMENT_ABANDONED
        or target.get("profile_establishment") == "abandoned_no_real_source"
    ):
        return None
    raw = target.get("profile_abandonment")
    raw = raw if isinstance(raw, Mapping) else {}
    message = str(raw.get("message") or _PROFILE_ABANDONED_MESSAGE).strip()
    return {
        "status": PROFILE_ESTABLISHMENT_ABANDONED,
        "code": PROFILE_ABANDONED_CODE,
        "reason": PROFILE_ABANDONED_CODE,
        "message": message,
        "basis": str(raw.get("basis") or "missing_active_profile_v3").strip(),
        "abandonment_reason": str(
            raw.get("abandonment_reason") or raw.get("reason") or "profile_incomplete"
        ).strip(),
        "abandonment_policy": str(
            raw.get("abandonment_policy") or "non_priority_profile_only"
        ).strip(),
        "candidate_retention": str(
            raw.get("candidate_retention")
            or "valid_card_backed_questions_remain_general_pool"
        ).strip(),
    }


def d2_source_mode_status(
    coverage: Mapping[str, Any] | None, *, institution: str, subject: str,
    reference_year: int, mode: str, profile_available: bool,
    source_available: bool,
) -> dict[str, Any]:
    """Project one source-mode status without changing generation rules."""
    structural = bool(profile_available and source_available and mode in D2_SOURCE_MODES)
    status = "not_run"
    acceptance = "not_run"
    reason = None if structural else "missing_active_profile_v3"
    target = _coverage_target(coverage, institution=institution, subject=subject,
                              reference_year=reference_year)
    abandonment = _coverage_profile_abandonment(
        coverage,
        institution=institution,
        subject=subject,
        reference_year=reference_year,
    )
    if abandonment is not None and not profile_available:
        return {
            "available": False,
            "status": "not_applicable",
            "acceptance_status": "not_applicable",
            "reason": abandonment["message"],
            "reason_code": abandonment["code"],
            "profile_establishment": abandonment["status"],
            "profile_abandonment": abandonment,
        }
    raw_mode = target.get("source_modes", {}).get(mode) if isinstance(target, Mapping) else None
    if isinstance(raw_mode, Mapping):
        source_status = raw_mode.get("source_type_preflight", "not_run")
        probe = raw_mode.get("solver_probe")
        probe_status = probe.get("status") if isinstance(probe, Mapping) else None
        status = probe_status or source_status or "not_run"
        if status == "source_types_passed":
            status = "not_run"
        if status in {"infeasible", "needs_candidate_review", "passed", "not_completed"}:
            acceptance = "local_preflight"
        # A successful representative run is stronger evidence than an
        # unfinished bounded probe.  Keep the probe status visible while
        # exposing the separately recorded real-acceptance state to the page.
        real_acceptance = str(raw_mode.get("real_acceptance_status") or "").strip()
        if real_acceptance:
            acceptance = real_acceptance
        witness = (probe.get("witness") if isinstance(probe, Mapping) else None) or raw_mode.get("source_type_witness")
        if status == "infeasible":
            structural = False
            reason_code = witness.get("reason") if isinstance(witness, Mapping) else None
            if reason_code == "question_type_total_capacity_shortfall":
                family = witness.get("question_type_family", "")
                reason = f"当前题源池无法满足{family}题型的总容量要求。"
            elif reason_code == "source_type_minimum_exceeds_quota":
                reason = "当前题源池无法满足该方式的题型与题源配额要求。"
            else:
                reason = "当前题源方式未通过本地可行性预检。"
        elif status == "needs_candidate_review":
            reason = "有界候选图尚未证明可行，正式提交仍会按当前规则复核。"
        elif status == "not_completed" and not real_acceptance:
            reason = "本地求解在预检预算内未完成，正式提交仍会按当前规则复核。"
        elif status == "passed":
            reason = None
    result = {
        "available": bool(structural),
        "status": status,
        "acceptance_status": acceptance,
        "reason": reason,
    }
    if isinstance(raw_mode, Mapping):
        exam_id = raw_mode.get("real_exam_id")
        if isinstance(exam_id, int) and not isinstance(exam_id, bool) and exam_id > 0:
            result["real_exam_id"] = exam_id
        job_id = raw_mode.get("real_job_id")
        if isinstance(job_id, str) and job_id.strip():
            result["real_job_id"] = job_id.strip()
    return result


def _catalog_subject_id(subject_ids: Mapping[str, object], subject: str) -> int:
    value = subject_ids.get(subject)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "已激活画像的学科未在本地目录登记",
            details={"reason": "profile_subject_not_in_catalog"},
        )
    return value


def _public_item(
    resolved: Mapping[str, Any],
    subject_ids: Mapping[str, object],
) -> dict[str, Any]:
    profile = resolved["profile"]
    identity = profile["identity"]
    slot_counts = Counter(slot["section_id"] for slot in profile["slots"])
    sections = [
        {
            "section_id": section["section_id"],
            "label": section["title"],
            "question_count": slot_counts[section["section_id"]],
        }
        for section in profile["sections"]
    ]
    return {
        "institution": identity["institution"],
        "subject": identity["subject"],
        "subject_id": _catalog_subject_id(subject_ids, identity["subject"]),
        "reference_year": identity["reference_year"],
        "combination_key": resolved["combination_key"],
        "activation_id": resolved["activation_id"],
        "profile_id": profile["profile_id"],
        "entry_revision": resolved["entry_revision"],
        "slot_count": len(profile["slots"]),
        "scoring_mode": profile["scoring"]["mode"],
        "learning_years": profile['provenance']['learning_years'],
        "sections": sections,
    }


def _raise_snapshot_database_error(exc: sqlite3.DatabaseError) -> None:
    message = str(exc).casefold()
    busy = isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )
    raise ProfileActivationStoreError(
        "profile_activation_store_busy"
        if busy
        else "profile_activation_store_failed",
        "画像激活状态暂时不可读取"
        if busy
        else "画像激活状态不可读取",
        retryable=busy,
    ) from exc


def load_profile_availability_snapshot(db) -> dict[str, Any]:
    """Read activation state and subject catalog from one SQLite snapshot."""

    owns_snapshot = not db.in_transaction
    try:
        if owns_snapshot:
            db.execute("BEGIN")
        state = load_activation_state_from_database(db)
        return {
            "registry": state["registry"],
            "artifact_map": state["artifact_map"],
            "subject_ids": subject_ids_by_name(db),
        }
    except (ProfileActivationStoreError, ProfileV3ContractError):
        raise
    except sqlite3.DatabaseError as exc:
        _raise_snapshot_database_error(exc)
    finally:
        if owns_snapshot and db.in_transaction:
            db.rollback()
    raise AssertionError("unreachable")


def build_profile_availability_payload(
    db,
    *,
    requested_combination: object | None = None,
) -> dict[str, Any]:
    """Return active combinations only, all from one validated DB snapshot."""

    requested = (
        normalize_profile_combination(requested_combination)
        if requested_combination is not None
        else None
    )
    snapshot = load_profile_availability_snapshot(db)
    registry = snapshot["registry"]
    artifacts = snapshot["artifact_map"]
    subject_ids = snapshot["subject_ids"]
    items: list[dict[str, Any]] = []
    for entry in registry["entries"].values():
        resolved = resolve_selectable_profile(
            registry,
            artifacts,
            entry["combination"],
        )
        item = _public_item(resolved, subject_ids)
        if requested is None or all(
            item[key] == requested[key]
            for key in ("institution", "subject", "reference_year")
        ):
            items.append(item)
    items.sort(
        key=lambda item: (
            item["institution"],
            item["subject"],
            -item["reference_year"],
        )
    )
    available = bool(items)
    return {
        "schema_version": PROFILE_AVAILABILITY_SCHEMA_VERSION,
        "contract_version": PROFILE_AVAILABILITY_CONTRACT_VERSION,
        "available": available,
        "code": None if available else "unsupported_profile_combination",
        "reason": None if available else "missing_active_profile_v3",
        "retryable": False,
        "message": None if available else _EMPTY_MESSAGE,
        "generation_submission_supported": GENERATION_SUBMISSION_SUPPORTED,
        "items": items,
    }


def _legacy_year_decision(
    registry: object,
    artifacts: object,
    combination: Mapping[str, Any],
    evidence_directory: Path | None = None,
) -> dict[str, Any]:
    decision = profile_availability(registry, artifacts, combination)
    if not decision["available"]:
        return decision
    resolved = resolve_selectable_profile(registry, artifacts, combination)
    review = {}
    if evidence_directory is not None and evidence_directory.is_dir():
        evidence_id = resolved['profile']['provenance']['evidence_manifest_hash']
        try:
            evidence = json.loads((evidence_directory / f'{evidence_id}.json').read_text(encoding='utf-8'))
            candidate = evidence.get('review', {})
            if all(candidate.get(key) == value for key, value in combination.items()):
                review = candidate
        except (OSError, ValueError):
            # Optional display evidence must not change activation availability.
            pass
    return {
        **decision,
        "activation_id": resolved["activation_id"],
        "entry_revision": resolved["entry_revision"],
        "year_selection_reason": review.get('year_selection_reason'),
        "learning_years": resolved.get('profile', {}).get('provenance', {}).get('learning_years', []),
    }


def overlay_legacy_source_availability(
    source: Mapping[str, Any],
    registry: object,
    artifacts: object,
    *,
    evidence_directory: Path | None = None,
    d2_coverage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep source diagnostics but make persisted activation the usable gate."""

    item = deepcopy(dict(source))
    structures: list[dict[str, Any]] = []
    selectable_years: list[int] = []
    decisions: list[dict[str, Any]] = []
    abandonments_by_year: dict[str, dict[str, Any]] = {}
    for raw in item.get("year_structures") or []:
        structure = deepcopy(dict(raw))
        year = structure.get("year")
        combination = {
            "institution": item.get("institution"),
            "subject": item.get("subject"),
            "reference_year": year,
        }
        decision = _legacy_year_decision(registry, artifacts, combination, evidence_directory)
        abandonment = (
            _coverage_profile_abandonment(
                d2_coverage,
                institution=str(item.get("institution") or ""),
                subject=str(item.get("subject") or ""),
                reference_year=year
                if isinstance(year, int) and not isinstance(year, bool)
                else 0,
            )
            if not decision.get("available")
            else None
        )
        if abandonment is not None:
            decision = {
                **decision,
                "code": abandonment["code"],
                "reason": abandonment["code"],
                "message": abandonment["message"],
                "profile_establishment": abandonment["status"],
                "profile_abandonment": abandonment,
            }
            if isinstance(year, int) and not isinstance(year, bool):
                abandonments_by_year[str(year)] = abandonment
        structure['year_selection_reason'] = decision.get('year_selection_reason')
        structure['learning_years'] = decision.get('learning_years', [])
        source_available = bool(
            structure.get("available", structure.get("usable", True))
        )
        profile_selectable = source_available and decision["available"]
        legacy_usable = (
            profile_selectable and GENERATION_SUBMISSION_SUPPORTED
        )
        structure["source_available"] = source_available
        structure["profile_availability"] = decision
        if abandonment is not None:
            structure["profile_establishment"] = abandonment["status"]
            structure["profile_abandonment"] = abandonment
            structure["candidate_retention"] = abandonment["candidate_retention"]
        structure["profile_selectable"] = profile_selectable
        structure["available"] = legacy_usable
        structure["usable"] = legacy_usable
        structure["availability_reason"] = (
            None
            if legacy_usable
            else (
                decision.get("reason")
                if not decision["available"]
                else (
                    "past_source_unavailable"
                    if not source_available
                    else "generation_submission_not_supported"
                )
            )
        )
        # Keep all three D2 source modes visible from the same year/profile
        # decision.  A local coverage projection may refine a mode to a
        # known capacity conflict; absent that projection, retain the profile
        # gate and do not invent a preflight result.
        structure["source_modes"] = {
            mode: d2_source_mode_status(
                d2_coverage,
                institution=str(item.get("institution") or ""),
                subject=str(item.get("subject") or ""),
                reference_year=year if isinstance(year, int) and not isinstance(year, bool) else 0,
                mode=mode,
                profile_available=legacy_usable,
                source_available=source_available,
            )
            for mode in D2_SOURCE_MODES
        }
        if (
            profile_selectable
            and isinstance(year, int)
            and not isinstance(year, bool)
        ):
            selectable_years.append(year)
        decisions.append(decision)
        structures.append(structure)

    source_years = list(item.get("available_years") or [])
    profile_years = sorted(set(selectable_years), reverse=True)
    legacy_years = (
        profile_years if GENERATION_SUBMISSION_SUPPORTED else []
    )
    item["source_available_years"] = source_years
    item["year_structures"] = structures
    if abandonments_by_year:
        item["profile_abandonments"] = abandonments_by_year
    item["available_years"] = list(legacy_years)
    item["selectable_years"] = list(profile_years)
    item["latest_available_year"] = (
        item["available_years"][0] if item["available_years"] else None
    )
    # Explicit alias consumed by the D2 page and submission diagnostics.
    item["actual_reference_year"] = item["latest_available_year"]
    # Each entry describes its own source structure.  The top-level alias is
    # the currently selectable (latest) profile year; it must not overwrite
    # older raw structures with that year.
    for structure in structures:
        structure["actual_reference_year"] = structure.get("year")
    item["available"] = bool(item["available_years"])
    item["usable"] = item["available"]
    item["availability_reason"] = (
        None
        if item["available"]
        else (
            "generation_submission_not_supported"
            if profile_years
            else (
                next(iter(abandonments_by_year.values()))["code"]
                if abandonments_by_year
                else "missing_active_profile_v3"
            )
        )
    )
    item["profile_mode"] = (
        "abandoned"
        if abandonments_by_year and not profile_years
        else "persisted_active_v3"
    )
    first_failure = next(
        (decision for decision in decisions if not decision["available"]),
        None,
    )
    item["profile_availability"] = {
        "schema_version": PROFILE_AVAILABILITY_SCHEMA_VERSION,
        "contract_version": PROFILE_AVAILABILITY_CONTRACT_VERSION,
        "available": bool(profile_years),
        "code": (
            None
            if profile_years
            else (
                next(iter(abandonments_by_year.values()))["code"]
                if abandonments_by_year
                else (
                    first_failure.get("code")
                    if first_failure
                    else "unsupported_profile_combination"
                )
            )
        ),
        "reason": (
            None
            if profile_years
            else (
                next(iter(abandonments_by_year.values()))["code"]
                if abandonments_by_year
                else (
                    first_failure.get("reason")
                    if first_failure
                    else "missing_active_profile_v3"
                )
            )
        ),
        "retryable": False,
        "message": (
            None
            if profile_years
            else (
                next(iter(abandonments_by_year.values()))["message"]
                if abandonments_by_year
                else _EMPTY_MESSAGE
            )
        ),
    }
    if abandonments_by_year and not profile_years:
        item["profile_establishment"] = PROFILE_ESTABLISHMENT_ABANDONED
        item["profile_abandonment"] = next(iter(abandonments_by_year.values()))
        item["candidate_retention"] = item["profile_abandonment"]["candidate_retention"]
    item["generation_submission_supported"] = GENERATION_SUBMISSION_SUPPORTED
    return item


__all__ = [
    "D2_SOURCE_MODES",
    "GENERATION_SUBMISSION_SUPPORTED",
    "PROFILE_ABANDONED_CODE",
    "PROFILE_ESTABLISHMENT_ABANDONED",
    "PROFILE_AVAILABILITY_CONTRACT_VERSION",
    "PROFILE_AVAILABILITY_SCHEMA_VERSION",
    "build_profile_availability_payload",
    "d2_source_mode_status",
    "load_d2_coverage",
    "load_profile_availability_snapshot",
    "overlay_legacy_source_availability",
]
