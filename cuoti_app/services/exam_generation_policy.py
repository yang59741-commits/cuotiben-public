"""Mode-selection policy used by exam generation."""

from __future__ import annotations

from typing import Any, Mapping


def target_institution_uses_manual_profile(
    config: Mapping[str, Any],
    institution: str | None,
) -> bool:
    """Return whether an institution is protected by the external portrait."""

    target = str(config.get("SEMANTIC_HOME_INSTITUTION") or "").strip()
    return bool(target and str(institution or "").strip() == target)


__all__ = ["target_institution_uses_manual_profile", "should_regenerate_draft"]


def should_regenerate_draft(
    *, source: str, draft_attempt: int, error
) -> bool:
    return (
        source in {"free", "mistakes"}
        and draft_attempt == 1
        and str(error.code) in {"QUALITY_DRAFT_INVALID", "QUALITY_DRAFT_REJECTED"}
        and "training_target_mismatch" not in set(error.issue_codes)
    )
