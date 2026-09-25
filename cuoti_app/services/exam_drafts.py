"""Pure, serializable draft contracts shared by source flows and publication."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class DraftContractError(ValueError):
    """Raised when a flow draft contains runtime state or invalid data."""


def _json_value(value: Any, *, field_name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DraftContractError(
            f"draft 字段 {field_name} 必须是可审计 JSON 数据"
        ) from exc
    return json.loads(encoded)


@dataclass(frozen=True)
class FlowOutcome:
    """A provider-free, publication-ready flow result."""

    source: str
    title: str
    latex_body: str
    latex_source: str
    config: Mapping[str, Any] = field(default_factory=dict)
    structured_items: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    memory_events: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    usage_candidate_ids: Sequence[str] = field(default_factory=tuple)
    selected_usage_ids: Sequence[str] = field(default_factory=tuple)
    manifest_metadata: Mapping[str, Any] = field(default_factory=dict)
    source_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in {"free", "mistakes", "past"}:
            raise DraftContractError("draft source 无效")
        for field_name in ("title", "latex_body", "latex_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DraftContractError(f"draft 字段 {field_name} 不能为空")
        for field_name in (
            "config",
            "structured_items",
            "memory_events",
            "usage_candidate_ids",
            "selected_usage_ids",
            "manifest_metadata",
            "source_snapshot",
        ):
            normalized = _json_value(getattr(self, field_name), field_name=field_name)
            object.__setattr__(self, field_name, normalized)

    def to_payload(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation for audit/receipt."""

        return copy.deepcopy({
            "source": self.source,
            "title": self.title,
            "latex_body": self.latex_body,
            "latex_source": self.latex_source,
            "config": self.config,
            "structured_items": self.structured_items,
            "memory_events": self.memory_events,
            "usage_candidate_ids": self.usage_candidate_ids,
            "selected_usage_ids": self.selected_usage_ids,
            "manifest_metadata": self.manifest_metadata,
            "source_snapshot": self.source_snapshot,
        })
