"""Persistent, versioned profiles of a formal past-exam source.

The profile stores only validated structural observations and evidence
metadata.  Original question bodies remain in the source snapshot and are
still used for candidate material reading and final selection.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROFILE_SCHEMA_VERSION = 2
PROFILE_PROMPT_VERSION = "past-exam-profile-v2"
PROFILE_ORIGIN = "manual_strong_model_review"
# The application does not produce these characterizations.  Keep the
# producer label independent from the currently configured in-app provider so
# switching providers cannot make an already verified artifact disappear.
DEFAULT_MODEL_PROFILE = "manual_strong_model"
SUPPORTED_PROFILE_PROMPTS = frozenset({"past-exam-profile-v1", PROFILE_PROMPT_VERSION})


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        (value if isinstance(value, str) else _canonical(value)).encode("utf-8")
    ).hexdigest()


def profile_identity(
    *,
    source_snapshot: str,
    question_bank_snapshot: str,
    institution: str,
    subject: str,
    reference_year: int,
    learning_years: Sequence[int],
    model_profile: str = DEFAULT_MODEL_PROFILE,
) -> dict[str, Any]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "prompt_version": PROFILE_PROMPT_VERSION,
        "source_snapshot": str(source_snapshot),
        "question_bank_snapshot": str(question_bank_snapshot),
        "institution": str(institution),
        "subject": str(subject),
        "reference_year": int(reference_year),
        "learning_years": [int(year) for year in learning_years],
        "model_profile": str(model_profile or DEFAULT_MODEL_PROFILE),
        "origin": PROFILE_ORIGIN,
    }


def profile_path(root: str | Path, identity: Mapping[str, Any]) -> Path:
    # Ordinary question-bank snapshots are contextual inputs, not part of the
    # durable characterization identity.  This allows a verified profile to
    # survive a rebuilt ordinary bank while its source evidence is unchanged.
    stable_identity = {
        key: identity.get(key)
        for key in (
            "schema_version", "prompt_version", "source_snapshot", "institution",
            "subject", "reference_year", "learning_years", "origin",
        )
    }
    key = _digest(stable_identity)
    return Path(root) / f"{key}.json"


def _legacy_profile_path(root: str | Path, identity: Mapping[str, Any]) -> Path:
    legacy = {
        key: identity.get(key)
        for key in (
            "source_snapshot", "question_bank_snapshot", "institution", "subject",
            "reference_year", "learning_years", "model_profile",
        )
    }
    legacy.update({"schema_version": 1, "prompt_version": "past-exam-profile-v1"})
    return Path(root) / f"{_digest(legacy)}.json"


def load_profile(root: str | Path, identity: Mapping[str, Any]) -> dict[str, Any] | None:
    root_path = Path(root)
    paths = [profile_path(root_path, identity), _legacy_profile_path(root_path, identity)]
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    # New profiles have a provider-independent path.  The fallback scan keeps
    # schema-v1 artifacts (whose filename included model_profile) usable after
    # a provider switch, without making the runtime call an AI model.
    for path in sorted(root_path.glob("*.json")) if root_path.is_dir() else ():
        if path not in paths:
            paths.append(path)
    for path in dict.fromkeys(paths):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        stored = value.get("identity")
        if not isinstance(stored, dict):
            continue
        # Do not accept an arbitrary JSON file merely because its subject and
        # years happen to match.  Schema-v1 artifacts remain supported for the
        # existing checked-in portraits, while newer artifacts must identify
        # the profile contract explicitly.
        try:
            stored_schema = int(stored.get("schema_version"))
        except (TypeError, ValueError):
            continue
        if stored_schema not in {1, PROFILE_SCHEMA_VERSION}:
            continue
        if str(stored.get("prompt_version") or "") not in SUPPORTED_PROFILE_PROMPTS:
            continue
        required = ("source_snapshot", "institution", "subject", "reference_year", "learning_years")
        if any(stored.get(key) != identity.get(key) for key in required):
            continue
        # Provenance is part of the trust boundary: a merely verified JSON
        # blob without an explicit producer marker must never become the
        # target institution's authoritative portrait.  Keep accepting the
        # checked-in v1 files, which store the marker at the payload level,
        # while requiring one of the two supported locations to be explicit.
        payload_origin = value.get("origin")
        identity_origin = stored.get("origin")
        if payload_origin is not None and payload_origin != PROFILE_ORIGIN:
            continue
        if identity_origin is not None and identity_origin != PROFILE_ORIGIN:
            continue
        if payload_origin != PROFILE_ORIGIN and identity_origin != PROFILE_ORIGIN:
            continue
        # Verification is an explicit provenance requirement.  Missing is not
        # equivalent to true; otherwise an old or hand-edited JSON file could
        # silently become the target institution's authoritative portrait.
        if value.get("verified") is not True:
            continue
        analysis = value.get("analysis")
        chapter_plan = value.get("chapter_plan")
        if not isinstance(analysis, dict) or not isinstance(chapter_plan, list):
            continue
        # The public save API stores the plan at the profile root for easy
        # inspection, while the generation contract validates it inside the
        # analysis object.  Normalize older/manual artifacts at the trust
        # boundary so the two representations cannot drift apart.
        if not isinstance(analysis.get("chapter_plan"), list):
            analysis = dict(analysis)
            analysis["chapter_plan"] = [dict(item) for item in chapter_plan]
        # Prefer an exact producer label and matching contextual bank snapshot,
        # but accept a different label because it is metadata, not a runtime
        # model contract.  Sorting makes multiple legacy artifacts deterministic.
        score = 0
        if str(stored.get("model_profile") or "") == str(identity.get("model_profile") or ""):
            score += 4
        if str(stored.get("question_bank_snapshot") or "") == str(identity.get("question_bank_snapshot") or ""):
            score += 2
        if path == paths[0]:
            score += 8
        candidates.append((score, path, value))
    if not candidates:
        return None
    _score, path, value = max(
        candidates,
        key=lambda item: (item[0], str(item[1])),
    )
    normalized = dict(value)
    normalized_analysis = normalized.get("analysis")
    normalized_plan = normalized.get("chapter_plan")
    if isinstance(normalized_analysis, dict) and isinstance(normalized_plan, list):
        if not isinstance(normalized_analysis.get("chapter_plan"), list):
            normalized_analysis = dict(normalized_analysis)
            normalized_analysis["chapter_plan"] = list(normalized_plan)
        normalized["analysis"] = normalized_analysis
    normalized["origin"] = PROFILE_ORIGIN
    normalized["verified"] = True
    normalized["profile_id"] = path.stem
    normalized["profile_path"] = str(path)
    return normalized


def material_understanding_from_profile(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    snapshot: str,
) -> dict[str, Any]:
    """Build a read-only material summary from a verified external portrait.

    The target institution's structural portrait is authored outside the app.
    Reusing it here avoids another in-app AI pass over the same historical
    papers while retaining the response shape consumed by the generation UI.
    Candidate-level cards for the final pool are still handled separately.
    """

    analysis = profile.get("analysis")
    analysis = dict(analysis) if isinstance(analysis, Mapping) else {}
    ordered_candidates = [
        item for item in candidates if isinstance(item, Mapping)
    ]
    chapters = sorted({str(item.get("chapter") or "未归类") for item in ordered_candidates})
    years = sorted({
        int(item["year"])
        for item in ordered_candidates
        if item.get("year") is not None
    })
    sources: dict[str, dict[str, int]] = {}
    for item in ordered_candidates:
        source = str(item.get("source_file") or "未知来源")
        sources.setdefault(source, {"count": 0})["count"] += 1
    section_summary = []
    for section in analysis.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        section_summary.append({
            "chapter": str(section.get("title") or "未命名板块"),
            "coverage": [str(section.get("role") or "")],
            "methods": [],
            "difficulty_distribution": {
                "portrait": str(section.get("difficulty") or "")
            },
            "duplicate_groups": [],
            "rare_types": [],
            "recommendations": [],
            "evidence_ids": list(analysis.get("evidence_ids") or []),
        })
    summary = {
        "chapters": section_summary,
        "global": {
            "quota_guidance": list(analysis.get("topic_distribution") or []),
            "difficulty_gradient": [str(analysis.get("difficulty_progression") or "")],
            "evidence_ids": list(analysis.get("evidence_ids") or []),
        },
        "portrait": analysis,
        "profile_reused": True,
    }
    profile_id = str(profile.get("profile_id") or "external-portrait")
    return {
        "schema_version": 1,
        "complete": True,
        "cache_key": f"profile:{profile_id}",
        "snapshot": str(snapshot),
        "total_count": len(ordered_candidates),
        "analyzed_count": len(ordered_candidates),
        "batch_count": 0,
        "reused_count": len(ordered_candidates),
        "chapters": chapters,
        "years": years,
        "sources": sources,
        "cards": [],
        "summary": summary,
        "profile_id": profile_id,
        "profile_origin": PROFILE_ORIGIN,
    }


def save_profile(
    root: str | Path,
    identity: Mapping[str, Any],
    analysis: Mapping[str, Any],
    chapter_plan: Sequence[Mapping[str, Any]],
) -> Path:
    path = profile_path(root, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_plan = [dict(item) for item in chapter_plan]
    normalized_analysis = dict(analysis)
    normalized_analysis["chapter_plan"] = [dict(item) for item in normalized_plan]
    payload = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_type": "past_exam_characterization",
        "profile_id": path.stem,
        "origin": PROFILE_ORIGIN,
        "verified": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identity": dict(identity),
        "analysis": normalized_analysis,
        "chapter_plan": normalized_plan,
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
