"""Privacy-safe, evidence-backed memory for the three isolated exam roles."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from ..utils import file_sha256
from .local_file_lock import file_lock as _file_lock
from .exam_generation_legacy_mistake_actor_projection import (
    build_legacy_mistake_actor_projection,
    projected_actor_map,
    validate_legacy_mistake_actor_projection,
)
from .exam_generation_mode_memory_authority import MemoryAuthorityError


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 3
PROFILE_SCHEMA_VERSION = 4
PROFILE_ENTRY_TYPES = frozenset({
    "explicit_preference",
    "inferred_weakness",
    "mastery_signal",
    "model_observation",
})
PROFILE_STATUSES = frozenset({
    "recent", "stable", "confirmed", "rejected", "suppressed"
})
MEMORY_TYPES = frozenset({"free", "mistakes", "past"})
READ_ROLES = {
    "free_exam": frozenset({"free", "mistakes"}),
    "mistake_analysis": frozenset(),
    "mistake_exam": frozenset({"mistakes"}),
    "past_exam": frozenset({"mistakes", "past"}),
}
WRITE_ROLES = {
    "free_exam": frozenset({"free"}),
    "mistake_analysis": frozenset({"mistakes"}),
    "mistake_exam": frozenset({"mistakes"}),
    "past_exam": frozenset({"past"}),
}
PREFERENCE_READ_ACTORS = {
    ("mistakes", "free_exam"): frozenset({"mistake_analysis"}),
    ("mistakes", "mistake_exam"): frozenset({"mistake_analysis"}),
    ("mistakes", "past_exam"): frozenset({"mistake_analysis"}),
}
_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEXT_LIMIT = 500
_LIST_LIMIT = 100


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _date_string(value: date | datetime | str | None = None) -> str:
    if value is None:
        return _local_now().date().isoformat()
    if isinstance(value, datetime):
        return value.astimezone().date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not _SAFE_DATE.fullmatch(text):
        raise ValueError("记忆日期必须是 YYYY-MM-DD")
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_json(path: Path, value: Any, *, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    if durable:
        with temp.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    os.replace(temp, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(value, encoding="utf-8", newline="\n")
    with temp.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _safe_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _safe_list(value: Any, limit: int = _LIST_LIMIT) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_safe_text(item, 160) for item in value[:limit] if _safe_text(item, 160)]


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _event_id(
    event: dict[str, Any],
    *,
    memory_type: str = "",
    actor: str = "",
    day: str = "",
) -> str:
    explicit = _safe_text(event.get("event_id"), 160)
    if explicit:
        return explicit
    payload = {
        "date": day,
        "actor": actor,
        "namespace": memory_type,
        "scope": _safe_text(
            event.get("operation_id")
            or event.get("exam_id")
            or event.get("mistake_public_id")
            or event.get("date_scope"),
            160,
        ),
        "event": dict(event),
    }
    payload["event"].pop("event_id", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _normalise_event(memory_type: str, actor: str, event: dict[str, Any], day: str) -> dict[str, Any]:
    """Whitelist facts and observations; never retain prompts, answers or solutions."""
    request = event.get("request") if isinstance(event.get("request"), dict) else {}
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    behavior = event.get("user_behavior") if isinstance(event.get("user_behavior"), dict) else {}
    event_kind = _safe_text(event.get("event_kind") or event.get("event_type") or "event", 80)
    raw_subjects = event.get("subjects")
    subjects = _safe_list(raw_subjects) if isinstance(raw_subjects, (list, tuple)) else []
    if not subjects and event.get("subject"):
        subjects = [_safe_text(event.get("subject"), 160)]
    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _event_id(
            event, memory_type=memory_type, actor=actor, day=day
        ),
        "occurred_at": _safe_text(event.get("occurred_at"), 80) or _local_now().isoformat(timespec="seconds"),
        "date": day,
        "memory_type": memory_type,
        "actor": actor,
        "event_kind": event_kind,
        "event_type": event_kind,
        "subject": subjects[0] if subjects else _safe_text(event.get("subject"), 160),
        "subjects": subjects,
        "chapters": _safe_list(event.get("chapters")),
        "source": _safe_text(event.get("source") or result.get("source") or memory_type, 80),
        "business_result": "published" if event_kind in {"request", "result"} and actor != "mistake_analysis" else "succeeded",
    }
    for key in ("operation_id", "paper_index"):
        if event.get(key) is not None:
            normalized[key] = _safe_text(event.get(key), 160) if key == "operation_id" else event.get(key)
    normalized["request"] = {
        key: (_safe_list(request.get(key)) if key == "types" else request.get(key) if key in {"question_count"} else _safe_text(request.get(key), 160))
        for key in ("difficulty", "target", "question_count", "types")
        if request.get(key) not in (None, "", [])
    }
    questions = []
    for item in (event.get("questions") or [])[:_LIST_LIMIT]:
        if not isinstance(item, dict):
            continue
        safe = {}
        # Legacy callers used ``id``; retain only its safe identifier meaning.
        question_id = item.get("question_id") or item.get("id")
        for key in ("candidate_id", "question_id", "fingerprint", "source_file", "source_number", "difficulty", "position", "adapted", "chapter", "subject"):
            if key == "question_id" and question_id is not None:
                safe[key] = _safe_text(question_id, 240)
                continue
            if item.get(key) is not None and item.get(key) != "":
                safe[key] = item.get(key) if key in {"position", "adapted"} else _safe_text(item.get(key), 240)
        for key in ("reference_ids",):
            values = _safe_list(item.get(key))
            if values:
                safe[key] = values
        if safe:
            questions.append(safe)
    normalized["questions"] = questions
    safe_result = {}
    for key in ("exam_id", "title", "source", "summary", "key_concept", "trap", "suggestion"):
        if result.get(key) not in (None, ""):
            safe_result[key] = _safe_text(result.get(key), _TEXT_LIMIT)
    if result.get("learning_years"):
        safe_result["learning_years"] = _safe_list(result.get("learning_years"))
    if result.get("reasons"):
        safe_result["reasons"] = _safe_list(result.get("reasons"))
    if safe_result:
        normalized["observation" if actor == "mistake_analysis" else "result"] = safe_result
    for key in (
        "fact_batch_hash",
        "publication_hash",
        "commit_authority_hash",
        "outbox_id",
        "envelope_hash",
    ):
        if event.get(key) not in (None, ""):
            normalized[key] = _safe_text(event.get(key), 64)
    publication_facts = []
    for item in (event.get("publication_facts") or [])[:_LIST_LIMIT]:
        if not isinstance(item, dict):
            continue
        safe_fact = {}
        for key in (
            "fact_type",
            "namespace",
            "actor",
            "candidate_id",
            "idempotency_key",
            "fact_hash",
            "candidate_fact_hash",
            "content_hash",
            "lineage_hash",
            "m_once_identity_hash",
        ):
            if item.get(key) not in (None, ""):
                safe_fact[key] = _safe_text(item.get(key), 512 if key == "candidate_id" else 80)
        if type(item.get("position")) is int and item["position"] > 0:
            safe_fact["position"] = item["position"]
        if safe_fact:
            publication_facts.append(safe_fact)
    if publication_facts:
        normalized["publication_facts"] = publication_facts
    selected = _safe_list(behavior.get("selected_mistake_ids"))
    if selected or behavior.get("allow_adaptation") is not None:
        normalized["user_behavior"] = {
            "allow_adaptation": bool(behavior.get("allow_adaptation")),
            "selected_mistake_ids": selected,
        }
    review_result = _safe_text(
        event.get("review_result") or event.get("result_type"), 20
    )
    if event_kind == "review" or review_result in {"again", "pass"}:
        normalized["review"] = {
            "result": review_result if review_result in {"again", "pass"} else "",
            "stage": _safe_non_negative_int(event.get("review_stage")),
            "lapse_count": _safe_non_negative_int(event.get("lapse_count")),
            "correct_streak": _safe_non_negative_int(event.get("correct_streak")),
            "reasons": _safe_list(event.get("reasons")),
        }
    return _bound_event(normalized)


def _bound_event(event: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    """Apply a deterministic total-size budget after whitelist filtering."""
    budget = max(500, int(limit or 20_000))
    value = dict(event)

    def size() -> int:
        return len(_canonical(value))

    questions = list(value.get("questions") or [])
    while size() > budget and questions:
        questions.pop()
        value["questions"] = questions
    for key in ("chapters", "subjects"):
        values = list(value.get(key) or [])
        while size() > budget and values:
            values.pop()
            value[key] = values
    if size() > budget:
        result_key = "observation" if "observation" in value else "result"
        result = value.get(result_key)
        if isinstance(result, dict):
            for key in ("summary", "suggestion", "trap", "key_concept", "title"):
                if key in result and size() > budget:
                    result[key] = _safe_text(result[key], 80)
            if size() > budget:
                value.pop(result_key, None)
    review = value.get("review")
    if size() > budget and isinstance(review, dict):
        review["reasons"] = []
    if size() > budget:
        # Keep the identity and routing facts even at very small configured limits.
        minimal = {key: value.get(key) for key in (
            "schema_version", "event_id", "occurred_at", "date", "memory_type",
            "actor", "event_kind", "event_type", "subject", "subjects", "source",
            "operation_id", "paper_index", "business_result", "review",
            "fact_batch_hash", "publication_hash", "commit_authority_hash",
            "outbox_id", "envelope_hash", "publication_facts",
        ) if value.get(key) not in (None, "", [], {})}
        minimal["questions"] = []
        value = minimal
    return value


class AIMemory:
    """Role-aware, privacy-safe event store with rebuildable projections."""

    def __init__(self, root: str | Path, *, daily_days: int = 30, max_event_chars: int = 20_000, max_context_chars: int = 12_000) -> None:
        self.root = Path(root).expanduser().resolve()
        self.daily_days = max(1, int(daily_days))
        self.max_event_chars = max(500, int(max_event_chars))
        self.max_context_chars = max(1000, int(max_context_chars))
        self._event_id_cache: dict[Path, tuple[int, int, set[str]]] = {}
        self._summary_cache: dict[Path, tuple[int, int, dict[str, Any]]] = {}

    def _namespace(self, memory_type: str) -> Path:
        if memory_type not in MEMORY_TYPES:
            raise ValueError("未知记忆类型")
        path = self.root / memory_type
        (path / "events" / "daily").mkdir(parents=True, exist_ok=True)
        (path / "summaries" / "daily").mkdir(parents=True, exist_ok=True)
        (path / "daily").mkdir(parents=True, exist_ok=True)  # schema-1 compatibility view
        self._ensure_manifest(path, memory_type)
        return path

    def _ensure_manifest(self, namespace: Path, memory_type: str) -> None:
        manifest_path = namespace / "manifest.json"
        if manifest_path.exists():
            return
        legacy = []
        for path in sorted((namespace / "daily").glob("*.jsonl")):
            legacy.append({"path": str(path.relative_to(namespace)).replace("\\", "/"), "sha256": _sha256(path), "bytes": path.stat().st_size})
        long_path = namespace / "long_term.md"
        if long_path.exists():
            legacy.append({"path": "long_term.md", "sha256": _sha256(long_path), "bytes": long_path.stat().st_size})
        _atomic_json(manifest_path, {
            "schema_version": SCHEMA_VERSION,
            "memory_type": memory_type,
            "event_count": 0,
            "last_event_id": None,
            "paused": False,
            "created_at": _local_now().isoformat(timespec="seconds"),
            "legacy_inventory": legacy,
        })

    def _manifest(
        self, memory_type: str, *, strict: bool = False
    ) -> dict[str, Any]:
        path = self._namespace(memory_type) / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if strict:
                raise MemoryAuthorityError("ai_memory_manifest_unavailable")
            return {}
        except OSError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_manifest_unavailable") from exc
            return {}
        except json.JSONDecodeError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_manifest_corrupt") from exc
            return {}
        if not isinstance(value, dict):
            if strict:
                raise MemoryAuthorityError("ai_memory_manifest_corrupt")
            return {}
        return value

    @staticmethod
    def _strict_json_object(
        path: Path,
        *,
        unavailable_code: str,
        corrupt_code: str,
    ) -> dict[str, Any]:
        """Read authority material without creating or repairing anything."""

        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MemoryAuthorityError(unavailable_code) from exc
        except OSError as exc:
            raise MemoryAuthorityError(unavailable_code) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MemoryAuthorityError(corrupt_code) from exc
        if not isinstance(value, dict):
            raise MemoryAuthorityError(corrupt_code)
        return value

    @staticmethod
    def _namespace_file_inventory_read_only(
        namespace: Path,
    ) -> list[dict[str, Any]]:
        paths = [
            namespace / name
            for name in (
                "manifest.json",
                "profile.json",
                "contributions.json",
                "suppressed_exams.json",
                "profile_authority.json",
                "actor_projection_v1.json",
            )
            if (namespace / name).is_file()
        ]
        for folder in (namespace / "daily", namespace / "events" / "daily"):
            try:
                if folder.is_dir():
                    paths.extend(sorted(folder.glob("*.jsonl")))
            except OSError as exc:
                raise MemoryAuthorityError(
                    "ai_memory_snapshot_unavailable"
                ) from exc
        inventory = []
        for path in sorted(set(paths), key=lambda item: item.as_posix()):
            try:
                stat = path.stat()
                digest = _sha256(path)
            except OSError as exc:
                raise MemoryAuthorityError(
                    "ai_memory_snapshot_unavailable"
                ) from exc
            inventory.append(
                {
                    "path": path.relative_to(namespace).as_posix(),
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": digest,
                }
            )
        return inventory

    def _attest_namespace_read_only(self, memory_type: str) -> dict[str, Any]:
        """Prove a readable namespace without mkdir, rebuild, or compatibility writes."""

        if memory_type not in MEMORY_TYPES:
            raise ValueError("未知记忆类型")
        namespace = self.root / memory_type
        if not namespace.is_dir():
            raise MemoryAuthorityError("ai_memory_namespace_unavailable")
        initial_file_inventory = self._namespace_file_inventory_read_only(
            namespace
        )

        manifest = self._strict_json_object(
            namespace / "manifest.json",
            unavailable_code="ai_memory_manifest_unavailable",
            corrupt_code="ai_memory_manifest_corrupt",
        )
        if (
            manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("memory_type") != memory_type
            or type(manifest.get("event_count")) is not int
            or manifest["event_count"] < 0
            or manifest.get("last_event_id") is not None
            and not isinstance(manifest.get("last_event_id"), str)
        ):
            raise MemoryAuthorityError("ai_memory_manifest_corrupt")

        raw_events: dict[str, dict[str, Any]] = {}
        for folder in (namespace / "daily", namespace / "events" / "daily"):
            try:
                paths = sorted(folder.glob("*.jsonl")) if folder.is_dir() else []
            except OSError as exc:
                raise MemoryAuthorityError("ai_memory_namespace_unavailable") from exc
            for path in paths:
                for raw in self._read_jsonl(path, strict=True):
                    event_id = str(raw.get("event_id") or _event_id(raw))
                    if event_id in raw_events and _canonical(
                        raw_events[event_id]
                    ) != _canonical(raw):
                        differing = {
                            key
                            for key in set(raw_events[event_id]) | set(raw)
                            if raw_events[event_id].get(key) != raw.get(key)
                        }
                        legacy_temporal_only = differing and differing.issubset(
                            {"date", "occurred_at"}
                        )
                        if not legacy_temporal_only:
                            raise MemoryAuthorityError(
                                "ai_memory_event_identity_corrupt"
                            )
                    raw_events[event_id] = raw

        suppression_path = namespace / "suppressed_exams.json"
        suppressed: set[str] = set()
        if suppression_path.exists():
            suppression = self._strict_json_object(
                suppression_path,
                unavailable_code="ai_memory_suppression_unavailable",
                corrupt_code="ai_memory_suppression_corrupt",
            )
            values = suppression.get("exam_ids")
            if not isinstance(values, list):
                raise MemoryAuthorityError("ai_memory_suppression_corrupt")
            suppressed = {
                str(value) for value in values if str(value).strip()
            }

        contributions_path = namespace / "contributions.json"
        if contributions_path.exists():
            contribution_document = self._strict_json_object(
                contributions_path,
                unavailable_code="ai_memory_contributions_unavailable",
                corrupt_code="ai_memory_contributions_corrupt",
            )
            contribution_values = contribution_document.get("entries")
            if (
                contribution_document.get("schema_version")
                != PROFILE_SCHEMA_VERSION
                or not isinstance(contribution_values, list)
                or not all(
                isinstance(item, dict) for item in contribution_values
                )
            ):
                raise MemoryAuthorityError("ai_memory_contributions_corrupt")
            contributions = [dict(item) for item in contribution_values]
        elif raw_events:
            raise MemoryAuthorityError("ai_memory_contributions_unavailable")
        else:
            contributions = []

        operation_exams: dict[str, set[str]] = {}
        for item in contributions:
            if item.get("kind") != "__exam_scope__":
                continue
            operation = _safe_text(item.get("operation_id"), 160)
            exam_id = _safe_text(item.get("value"), 160)
            if operation and exam_id:
                operation_exams.setdefault(operation, set()).add(exam_id)
        fully_suppressed_operations = {
            operation
            for operation, exam_ids in operation_exams.items()
            if exam_ids and exam_ids.issubset(suppressed)
        }

        events: list[dict[str, Any]] = []
        projection_events: list[dict[str, Any]] = []
        for raw in raw_events.values():
            try:
                schema_version = int(raw.get("schema_version") or 1)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MemoryAuthorityError("ai_memory_jsonl_corrupt") from exc
            event = raw
            if schema_version < SCHEMA_VERSION:
                event = _normalise_event(
                    memory_type,
                    _safe_text(raw.get("actor"), 80),
                    raw,
                    _safe_text(raw.get("date"), 10),
                )
            projection_events.append(event)
            if self._event_exam_id(event) in suppressed or (
                not self._event_exam_id(event)
                and _safe_text(event.get("operation_id"), 160)
                in fully_suppressed_operations
            ):
                continue
            events.append(event)
        events.sort(
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                str(item.get("event_id") or ""),
            )
        )
        expected_last = str(events[-1].get("event_id") or "") if events else None
        if (
            manifest["event_count"] != len(events)
            or manifest.get("last_event_id") != expected_last
        ):
            raise MemoryAuthorityError("ai_memory_manifest_inventory_drift")

        profile_path = namespace / "profile.json"
        if profile_path.exists():
            profile = self._strict_json_object(
                profile_path,
                unavailable_code="ai_memory_profile_unavailable",
                corrupt_code="ai_memory_profile_corrupt",
            )
            profile_entries = profile.get("entries")
            if (
                profile.get("schema_version") != PROFILE_SCHEMA_VERSION
                or not isinstance(profile_entries, list)
                or not all(
                isinstance(item, dict) for item in profile_entries
                )
            ):
                raise MemoryAuthorityError("ai_memory_profile_corrupt")
        elif events:
            raise MemoryAuthorityError("ai_memory_profile_unavailable")
        else:
            profile = {"schema_version": PROFILE_SCHEMA_VERSION, "entries": []}

        authority_path = namespace / "profile_authority.json"
        if authority_path.exists():
            authority_document = self._strict_json_object(
                authority_path,
                unavailable_code="ai_memory_profile_authority_unavailable",
                corrupt_code="ai_memory_profile_authority_corrupt",
            )
            authority_values = authority_document.get("entries")
            if (
                authority_document.get("schema_version")
                != PROFILE_SCHEMA_VERSION
                or not isinstance(authority_values, list)
                or not all(
                isinstance(item, dict)
                and _safe_text(item.get("key"), 500)
                for item in authority_values
                )
            ):
                raise MemoryAuthorityError("ai_memory_profile_authority_corrupt")
        elif raw_events or profile.get("entries"):
            raise MemoryAuthorityError(
                "ai_memory_profile_authority_unavailable"
            )
        else:
            authority_document = {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "entries": [],
                "state": "absent",
            }
            authority_values = []
        profile_authority = {
            str(item["key"]): dict(item) for item in authority_values
        }
        self._validate_contribution_inventory_read_only(
            memory_type,
            projection_events,
            contributions,
            allow_archived_sources=manifest.get("projection_hashes")
            is not None,
        )
        self._validate_profile_projection_read_only(
            memory_type,
            profile,
            contributions,
            profile_authority,
            suppressed,
        )

        projected_actors: dict[str, str] = {}
        projection_path = namespace / "actor_projection_v1.json"
        if projection_path.exists():
            projection = self._strict_json_object(
                projection_path,
                unavailable_code="ai_memory_actor_projection_unavailable",
                corrupt_code="ai_memory_actor_projection_corrupt",
            )
            try:
                validated_projection = validate_legacy_mistake_actor_projection(
                    projection,
                    events=list(raw_events.values()),
                    projection_authority_ref=projection.get(
                        "projection_authority_ref"
                    ),
                )
            except MemoryAuthorityError as exc:
                raise MemoryAuthorityError(
                    "ai_memory_actor_projection_corrupt"
                ) from exc
            projected_actors = projected_actor_map(validated_projection)

        file_inventory = self._namespace_file_inventory_read_only(namespace)
        if _canonical(initial_file_inventory) != _canonical(file_inventory):
            raise MemoryAuthorityError("ai_memory_snapshot_unavailable")
        projection_hashes = manifest.get("projection_hashes")
        if projection_hashes is not None:
            file_hashes = {
                item["path"]: item["sha256"] for item in file_inventory
            }
            expected_projection_hashes = {
                "version": "ai-memory-projection-hashes-v1",
                "profile": file_hashes.get("profile.json"),
                "contributions": file_hashes.get("contributions.json"),
                "profile_authority": file_hashes.get(
                    "profile_authority.json"
                ),
                "suppression": file_hashes.get("suppressed_exams.json"),
            }
            if _canonical(projection_hashes) != _canonical(
                expected_projection_hashes
            ):
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )
        inventory_body = {
            "memory_type": memory_type,
            "schema_version": SCHEMA_VERSION,
            "event_count": len(events),
            "last_event_id": expected_last,
            "files": [
                {key: value for key, value in item.items() if key != "mtime_ns"}
                for item in file_inventory
            ],
        }
        return {
            "namespace": namespace,
            "manifest": manifest,
            "profile": profile,
            "events": events,
            "contributions": contributions,
            "suppressed_exam_ids": suppressed,
            "profile_authority": profile_authority,
            "profile_authority_hash": hashlib.sha256(
                _canonical(authority_document).encode("utf-8")
            ).hexdigest(),
            "projected_actors": projected_actors,
            "inventory_hash": hashlib.sha256(
                _canonical(inventory_body).encode("utf-8")
            ).hexdigest(),
        }

    def _event_path(self, memory_type: str, day: str) -> Path:
        _date_string(day)
        return self._namespace(memory_type) / "events" / "daily" / f"{day}.jsonl"

    def _compat_path(self, memory_type: str, day: str) -> Path:
        return self._namespace(memory_type) / "daily" / f"{day}.jsonl"

    def _suppression_path(self, memory_type: str) -> Path:
        return self._namespace(memory_type) / "suppressed_exams.json"

    def _contribution_path(self, memory_type: str) -> Path:
        return self._namespace(memory_type) / "contributions.json"

    def _profile_authority_path(self, memory_type: str) -> Path:
        return self._namespace(memory_type) / "profile_authority.json"

    def _actor_projection_path(self, memory_type: str) -> Path:
        return self._namespace(memory_type) / "actor_projection_v1.json"

    def install_legacy_mistake_actor_projection(
        self, *, projection_authority_ref: str
    ) -> dict[str, Any]:
        """Persist a read-only actor sidecar without rewriting source events."""

        events = self._raw_events("mistakes")
        receipt = build_legacy_mistake_actor_projection(
            events=events,
            projection_authority_ref=projection_authority_ref,
        )
        _atomic_json(self._actor_projection_path("mistakes"), receipt)
        return receipt

    def _legacy_actor_projection(self, memory_type: str) -> dict[str, str]:
        if memory_type != "mistakes":
            return {}
        try:
            raw = json.loads(
                self._actor_projection_path(memory_type).read_text(encoding="utf-8")
            )
            receipt = validate_legacy_mistake_actor_projection(
                raw,
                events=self._raw_events(memory_type),
                projection_authority_ref=raw.get("projection_authority_ref"),
            )
            return projected_actor_map(receipt)
        except (OSError, json.JSONDecodeError, MemoryAuthorityError):
            return {}

    def _suppressed_exam_ids(
        self, memory_type: str, *, strict: bool = False
    ) -> set[str]:
        path = self._suppression_path(memory_type)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except OSError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_suppression_unavailable") from exc
            return set()
        except json.JSONDecodeError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_suppression_corrupt") from exc
            return set()
        values = raw.get("exam_ids") if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            if strict:
                raise MemoryAuthorityError("ai_memory_suppression_corrupt")
            return set()
        return {str(value) for value in values or [] if str(value).strip()}

    def _load_profile_authority(
        self,
        memory_type: str,
        legacy_profile: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        path = self._profile_authority_path(memory_type)
        loaded_ok = False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded_ok = isinstance(raw, dict) and isinstance(raw.get("entries"), list)
        except (OSError, json.JSONDecodeError):
            raw = {}
        values = raw.get("entries") if isinstance(raw, dict) else []
        authority = {
            str(item.get("key")): dict(item)
            for item in values or []
            if isinstance(item, dict) and _safe_text(item.get("key"), 500)
        }
        # One-time compatibility lift: confirmed/rejected/suppressed and
        # corrections are user facts, not rebuildable projection state.
        migrated = False
        if not authority and isinstance(legacy_profile, dict):
            for item in legacy_profile.get("entries") or []:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "")
                correction = _safe_text(item.get("correction"), 500)
                if status not in {"confirmed", "rejected", "suppressed"} and not correction:
                    continue
                key = _safe_text(item.get("key"), 500)
                if not key:
                    continue
                authority[key] = {
                    key_name: item.get(key_name)
                    for key_name in (
                        "key", "value", "effective_value", "entry_type", "status",
                        "correction", "confidence", "evidence_count", "distinct_days",
                        "first_seen", "last_seen", "subjects", "chapters",
                        "user_updated_at",
                    )
                    if item.get(key_name) not in (None, "")
                }
                migrated = True
        if not loaded_ok or migrated:
            _atomic_json(
                path,
                {
                    "schema_version": PROFILE_SCHEMA_VERSION,
                    "entries": [authority[key] for key in sorted(authority)],
                },
            )
        return authority

    def _write_profile_authority(
        self, memory_type: str, authority: dict[str, dict[str, Any]]
    ) -> None:
        _atomic_json(
            self._profile_authority_path(memory_type),
            {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "entries": [authority[key] for key in sorted(authority)],
            },
        )

    @staticmethod
    def _event_exam_id(event: dict[str, Any]) -> str | None:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if not result and isinstance(event.get("observation"), dict):
            result = event["observation"]
        value = result.get("exam_id") or event.get("exam_id")
        return str(value) if value not in (None, "") else None

    def append_event(
        self,
        memory_type: str,
        actor: str,
        event: dict[str, Any],
        *,
        day: date | datetime | str | None = None,
        verify_duplicate_payload: bool = False,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the atomic logical batch API."""

        return self.append_events(
            memory_type,
            actor,
            [event],
            day=day,
            verify_duplicate_payload=verify_duplicate_payload,
        )[0]

    def append_events(
        self,
        memory_type: str,
        actor: str,
        events: Iterable[dict[str, Any]],
        *,
        day: date | datetime | str | None = None,
        verify_duplicate_payload: bool = False,
    ) -> list[dict[str, Any]]:
        """Validate and persist one logical batch with one lock and one fsync."""

        if memory_type not in WRITE_ROLES.get(actor, frozenset()):
            raise PermissionError(f"角色 {actor} 无权写入 {memory_type} 记忆")
        target_day = _date_string(day)
        raw_events = list(events)
        for event in raw_events:
            if not isinstance(event, dict):
                raise TypeError("记忆事件必须是对象")
        normalized_events = [
            _bound_event(
                _normalise_event(memory_type, actor, event, target_day),
                self.max_event_chars,
            )
            for event in raw_events
        ]
        if not normalized_events:
            return []
        if self._manifest(memory_type).get("paused"):
            return [{**event, "memory_paused": True} for event in normalized_events]
        path = self._event_path(memory_type, target_day)
        lock_path = self._namespace(memory_type) / ".memory.lock"
        with _file_lock(lock_path):
            if verify_duplicate_payload and path.exists():
                try:
                    raw_bytes = path.read_bytes()
                except OSError as exc:
                    raise MemoryAuthorityError("ai_memory_strict_jsonl_read_failed") from exc
                if raw_bytes and not raw_bytes.endswith(b"\n"):
                    boundary = raw_bytes.rfind(b"\n") + 1
                    partial = raw_bytes[boundary:]
                    expected_lines = [
                        (_canonical(item) + "\n").encode("utf-8")
                        for item in normalized_events
                    ]
                    matches = [line for line in expected_lines if line.startswith(partial)]
                    if len(matches) != 1:
                        raise MemoryAuthorityError("ai_memory_strict_jsonl_tail_conflict")
                    try:
                        with path.open("r+b") as handle:
                            handle.truncate(boundary)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except OSError as exc:
                        raise MemoryAuthorityError("ai_memory_strict_jsonl_tail_repair_failed") from exc
                    raw_bytes = raw_bytes[:boundary]
                try:
                    decoded_lines = raw_bytes.decode("utf-8").splitlines()
                    strict_values = [json.loads(line) for line in decoded_lines]
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MemoryAuthorityError("ai_memory_strict_jsonl_invalid") from exc
                if not all(
                    isinstance(item, dict) and str(item.get("event_id") or "")
                    for item in strict_values
                ):
                    raise MemoryAuthorityError("ai_memory_strict_jsonl_invalid")
            existing = self._event_ids(path)
            existing_payloads = (
                {
                    str(item.get("event_id") or ""): item
                    for item in self._read_jsonl(path)
                    if item.get("event_id")
                }
                if verify_duplicate_payload
                else {}
            )
            pending: list[dict[str, Any]] = []
            pending_by_id: dict[str, dict[str, Any]] = {}
            for normalized in normalized_events:
                event_id = str(normalized["event_id"])
                if event_id in existing:
                    if (
                        verify_duplicate_payload
                        and _canonical(existing_payloads.get(event_id))
                        != _canonical(normalized)
                    ):
                        raise MemoryAuthorityError("ai_memory_event_id_payload_conflict")
                    continue
                if event_id in pending_by_id:
                    if (
                        verify_duplicate_payload
                        and _canonical(pending_by_id[event_id]) != _canonical(normalized)
                    ):
                        raise MemoryAuthorityError("ai_memory_event_id_payload_conflict")
                    continue
                pending.append(normalized)
                pending_by_id[event_id] = normalized
            if pending:
                payload = "".join(_canonical(item) + "\n" for item in pending)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                prior_count = len(existing)
                existing.update(pending_by_id)
                stat = path.stat()
                self._event_id_cache[path] = (
                    stat.st_mtime_ns,
                    stat.st_size,
                    existing,
                )
                try:
                    for offset, normalized in enumerate(pending, start=1):
                        self._update_daily_summary(
                            memory_type,
                            target_day,
                            normalized,
                            expected_event_count=prior_count + offset,
                        )
                    # A batch refreshes the namespace projection only once.
                    self._rebuild_namespace(memory_type)
                except Exception:
                    # The fsynced event remains authoritative; projections can be
                    # rebuilt by startup maintenance without losing the event.
                    logger.exception("AI memory projection refresh failed memory_type=%s", memory_type)
                    manifest = self._manifest(memory_type)
                    manifest["profile_stale"] = True
                    manifest["updated_at"] = _local_now().isoformat(timespec="seconds")
                    _atomic_json(self._namespace(memory_type) / "manifest.json", manifest, durable=False)
        if day is None and pending:
            try:
                self.rollover_expired_days(memory_type)
            except Exception:
                logger.exception(
                    "AI memory retention rollover failed memory_type=%s",
                    memory_type,
                )
        return normalized_events

    @staticmethod
    def _read_jsonl(
        path: Path, *, strict: bool = False
    ) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_jsonl_unavailable") from exc
            return []
        values = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise MemoryAuthorityError("ai_memory_jsonl_corrupt") from exc
                continue
            if isinstance(item, dict):
                values.append(item)
            elif strict:
                raise MemoryAuthorityError("ai_memory_jsonl_corrupt")
        return values

    def _event_ids(self, path: Path) -> set[str]:
        try:
            stat = path.stat()
        except OSError:
            self._event_id_cache.pop(path, None)
            return set()
        cached = self._event_id_cache.get(path)
        signature = (stat.st_mtime_ns, stat.st_size)
        if cached is not None and cached[:2] == signature:
            return cached[2]
        values = {str(item.get("event_id")) for item in self._read_jsonl(path)}
        self._event_id_cache[path] = (*signature, values)
        return values

    def _all_events(self, memory_type: str) -> list[dict[str, Any]]:
        namespace = self._namespace(memory_type)
        suppressed = self._suppressed_exam_ids(memory_type)
        suppressed_operations = self._fully_suppressed_operations(memory_type)
        events: dict[str, dict[str, Any]] = {}
        for folder in (namespace / "daily", namespace / "events" / "daily"):
            for path in sorted(folder.glob("*.jsonl")):
                for raw in self._read_jsonl(path):
                    if int(raw.get("schema_version") or 1) < SCHEMA_VERSION:
                        raw = _normalise_event(memory_type, _safe_text(raw.get("actor"), 80), raw, _safe_text(raw.get("date"), 10) or path.stem)
                    if self._event_exam_id(raw) in suppressed or (
                        not self._event_exam_id(raw)
                        and _safe_text(raw.get("operation_id"), 160) in suppressed_operations
                    ):
                        continue
                    events[str(raw.get("event_id") or _event_id(raw))] = raw
        return sorted(events.values(), key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or "")))

    def _read_daily(
        self,
        memory_type: str,
        start: date,
        end: date,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        namespace = self._namespace(memory_type)
        suppressed = self._suppressed_exam_ids(memory_type, strict=strict)
        suppressed_operations = self._fully_suppressed_operations(
            memory_type, strict=strict
        )
        events: dict[str, dict[str, Any]] = {}
        current = start
        while current <= end:
            day = current.isoformat()
            for path in (
                namespace / "daily" / f"{day}.jsonl",
                namespace / "events" / "daily" / f"{day}.jsonl",
            ):
                for raw in self._read_jsonl(path, strict=strict):
                    if int(raw.get("schema_version") or 1) < SCHEMA_VERSION:
                        raw = _normalise_event(
                            memory_type,
                            _safe_text(raw.get("actor"), 80),
                            raw,
                            _safe_text(raw.get("date"), 10) or day,
                        )
                    if self._event_exam_id(raw) in suppressed or (
                        not self._event_exam_id(raw)
                        and _safe_text(raw.get("operation_id"), 160) in suppressed_operations
                    ):
                        continue
                    events[str(raw.get("event_id") or _event_id(raw))] = raw
            current += timedelta(days=1)
        return sorted(
            events.values(),
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                str(item.get("event_id") or ""),
            ),
        )

    @staticmethod
    def _evidence(event: dict[str, Any]) -> list[tuple[str, str]]:
        pairs = []
        subjects = _safe_list(event.get("subjects"))
        if not subjects:
            subject = _safe_text(event.get("subject"), 160)
            subjects = [subject] if subject else []
        pairs.extend(("subject", value) for value in subjects)
        pairs.extend(("chapter", value) for value in _safe_list(event.get("chapters")))
        request = event.get("request") if isinstance(event.get("request"), dict) else {}
        difficulty = _safe_text(request.get("difficulty"), 160)
        if difficulty:
            pairs.append(("difficulty", difficulty))
        pairs.extend(("question_type", value) for value in _safe_list(request.get("types")))
        return pairs

    @staticmethod
    def _typed_evidence(event: dict[str, Any]) -> list[tuple[str, str, str]]:
        actor = _safe_text(event.get("actor"), 80)
        event_kind = _safe_text(
            event.get("event_kind") or event.get("event_type"), 80
        )
        default_type = (
            "inferred_weakness"
            if actor == "mistake_analysis"
            else "model_observation"
        )
        facts = [
            (default_type, kind, value)
            for kind, value in AIMemory._evidence(event)
        ]
        result = event.get("observation") or event.get("result") or {}
        if isinstance(result, dict) and actor == "mistake_analysis":
            for field in ("key_concept", "trap"):
                value = _safe_text(result.get(field), 160)
                if value:
                    facts.append(("inferred_weakness", field, value))
            for value in _safe_list(result.get("reasons")):
                facts.append(("inferred_weakness", "reason", value))
        behavior = event.get("user_behavior") or {}
        if isinstance(behavior, dict) and behavior.get("allow_adaptation") is not None:
            facts.append((
                "explicit_preference",
                "allow_adaptation",
                "允许" if behavior.get("allow_adaptation") else "不允许",
            ))
        review = event.get("review") or {}
        if event_kind == "review" and isinstance(review, dict):
            result_value = _safe_text(review.get("result"), 20)
            if result_value in {"again", "pass"}:
                facts.append(("mastery_signal", "review_result", result_value))
            facts.append((
                "mastery_signal",
                "review_stage",
                str(_safe_non_negative_int(review.get("stage"))),
            ))
            if _safe_non_negative_int(review.get("lapse_count")) > 0:
                facts.append((
                    "mastery_signal",
                    "lapse_count",
                    str(_safe_non_negative_int(review.get("lapse_count"))),
                ))
            for value in _safe_list(review.get("reasons")):
                facts.append(("inferred_weakness", "reason", value))
        deduped = []
        seen = set()
        for entry_type, kind, value in facts:
            key = (entry_type, kind, value.casefold())
            if value and key not in seen:
                seen.add(key)
                deduped.append((entry_type, kind, value))
        return deduped

    def _load_contributions(
        self, memory_type: str, *, strict: bool = False
    ) -> list[dict[str, Any]]:
        path = self._contribution_path(memory_type)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except OSError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_contributions_unavailable") from exc
            return []
        except json.JSONDecodeError as exc:
            if strict:
                raise MemoryAuthorityError("ai_memory_contributions_corrupt") from exc
            return []
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            if strict:
                raise MemoryAuthorityError("ai_memory_contributions_corrupt")
            return []
        if strict and not all(isinstance(item, dict) for item in raw["entries"]):
            raise MemoryAuthorityError("ai_memory_contributions_corrupt")
        return [dict(item) for item in raw["entries"] if isinstance(item, dict)]

    @staticmethod
    def _contribution_source(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": str(event.get("event_id") or ""),
            "exam_id": AIMemory._event_exam_id(event),
            "date": _safe_text(event.get("date"), 10),
            "actor": _safe_text(event.get("actor"), 80),
            "subjects": _safe_list(event.get("subjects")),
            "chapters": _safe_list(event.get("chapters")),
        }

    @staticmethod
    def _normalise_contribution_sources(item: dict[str, Any]) -> list[dict[str, Any]]:
        raw_sources = item.get("sources")
        if isinstance(raw_sources, list):
            return [dict(source) for source in raw_sources if isinstance(source, dict)]
        # Compatibility with the first stage-C ledger draft.
        event_id = str(item.get("event_id") or "")
        if not event_id:
            return []
        return [{
            "event_id": event_id,
            "exam_id": item.get("exam_id"),
            "date": _safe_text(item.get("date"), 10),
            "actor": _safe_text(item.get("actor"), 80),
            "subjects": _safe_list(item.get("subjects")),
            "chapters": _safe_list(item.get("chapters")),
        }]

    def _validate_contribution_inventory_read_only(
        self,
        memory_type: str,
        raw_events: list[dict[str, Any]],
        contributions: list[dict[str, Any]],
        *,
        allow_archived_sources: bool,
    ) -> None:
        ledger: dict[str, dict[str, Any]] = {}
        for item in contributions:
            contribution_id = str(item.get("contribution_id") or "")
            raw_sources = item.get("sources")
            if (
                not contribution_id
                or contribution_id in ledger
                or not isinstance(raw_sources, list)
                or not raw_sources
                or not all(
                    isinstance(source, dict)
                    and _safe_text(source.get("event_id"), 160)
                    for source in raw_sources
                )
            ):
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )
            sources = self._normalise_contribution_sources(item)
            operation = _safe_text(item.get("operation_id"), 160)
            kind = _safe_text(item.get("kind"), 160)
            value = _safe_text(item.get("value"), 500)
            entry_type = _safe_text(item.get("entry_type"), 80)
            if kind == "__exam_scope__":
                identity = "|".join(
                    (memory_type, operation, "exam_scope", value)
                )
            else:
                scope = (
                    operation
                    or _safe_text(sources[0].get("exam_id"), 160)
                    or _safe_text(sources[0].get("event_id"), 160)
                )
                identity = "|".join(
                    (
                        memory_type,
                        scope,
                        entry_type,
                        kind,
                        value.casefold(),
                    )
                )
            expected_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if (
                contribution_id != expected_id
                or item.get("fact_key") != f"{kind}:{value}"
            ):
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )
            ledger[contribution_id] = item

        expected_live: dict[str, dict[str, dict[str, Any]]] = {}
        for event in raw_events:
            for expected in self._event_contributions(memory_type, event):
                contribution_id = str(expected["contribution_id"])
                target = expected_live.setdefault(contribution_id, {})
                for source in expected["sources"]:
                    target[str(source["event_id"])] = source
                actual = ledger.get(contribution_id)
                if actual is None or any(
                    actual.get(key) != expected.get(key)
                    for key in (
                        "operation_id",
                        "entry_type",
                        "kind",
                        "value",
                        "fact_key",
                    )
                ):
                    raise MemoryAuthorityError(
                        "ai_memory_projection_inventory_drift"
                    )
        live_event_ids = {
            str(event.get("event_id") or _event_id(event))
            for event in raw_events
        }
        if not allow_archived_sources and any(
            str(source.get("event_id") or "") not in live_event_ids
            for item in ledger.values()
            for source in self._normalise_contribution_sources(item)
        ):
            raise MemoryAuthorityError(
                "ai_memory_projection_inventory_drift"
            )
        for contribution_id, item in ledger.items():
            actual_sources = {
                str(source.get("event_id") or ""): source
                for source in self._normalise_contribution_sources(item)
            }
            for event_id in live_event_ids.intersection(actual_sources):
                expected_source = expected_live.get(contribution_id, {}).get(
                    event_id
                )
                if (
                    expected_source is None
                    or _canonical(actual_sources[event_id])
                    != _canonical(expected_source)
                ):
                    raise MemoryAuthorityError(
                        "ai_memory_projection_inventory_drift"
                    )
        for contribution_id, sources in expected_live.items():
            actual_sources = {
                str(source.get("event_id") or ""): source
                for source in self._normalise_contribution_sources(
                    ledger[contribution_id]
                )
            }
            if any(
                event_id not in actual_sources
                or _canonical(actual_sources[event_id]) != _canonical(source)
                for event_id, source in sources.items()
            ):
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )

    def _validate_profile_projection_read_only(
        self,
        memory_type: str,
        profile: dict[str, Any],
        contributions: list[dict[str, Any]],
        authority: dict[str, dict[str, Any]],
        suppressed: set[str],
    ) -> None:
        active = self._active_contributions(
            memory_type,
            contributions,
            strict=True,
            suppressed_exam_ids=suppressed,
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in active:
            grouped.setdefault(
                (
                    _safe_text(item.get("kind"), 160),
                    _safe_text(item.get("value"), 500),
                ),
                [],
            ).append(item)
        type_rank = {
            "explicit_preference": 0,
            "inferred_weakness": 1,
            "mastery_signal": 2,
            "model_observation": 3,
        }
        expected: dict[str, dict[str, Any]] = {}
        try:
            projection_day = datetime.fromisoformat(
                str(profile.get("updated_at") or "")
            ).date()
        except ValueError as exc:
            if grouped or authority:
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                ) from exc
            projection_day = _local_now().date()
        for (kind, value), raw_evidence in grouped.items():
            if not kind or not value:
                continue
            evidence_by_scope: dict[str, dict[str, Any]] = {}
            for item in raw_evidence:
                sources = self._normalise_contribution_sources(item)
                scope = (
                    _safe_text(item.get("operation_id"), 160)
                    or (_safe_text(sources[0].get("event_id"), 160) if sources else "")
                    or str(item.get("contribution_id") or "")
                )
                current = evidence_by_scope.get(scope)
                if current is None:
                    evidence_by_scope[scope] = dict(item)
                    continue
                merged_sources = {
                    str(source.get("event_id") or ""): source
                    for source in self._normalise_contribution_sources(current)
                }
                merged_sources.update(
                    {
                        str(source.get("event_id") or ""): source
                        for source in sources
                    }
                )
                current["sources"] = [
                    merged_sources[event_id]
                    for event_id in sorted(merged_sources)
                ]
                if type_rank.get(
                    str(item.get("entry_type") or ""), 99
                ) < type_rank.get(str(current.get("entry_type") or ""), 99):
                    current["entry_type"] = item.get("entry_type")
            evidence = list(evidence_by_scope.values())
            sources = [
                source
                for item in evidence
                for source in self._normalise_contribution_sources(item)
            ]
            days = []
            for source in sources:
                day = _safe_text(source.get("date"), 10)
                try:
                    days.append(_date_string(day))
                except ValueError:
                    continue
            days = sorted(set(days))
            if not days:
                continue
            key = f"{kind}:{value}"
            user_fact = authority.get(key, {})
            old_status = str(user_fact.get("status") or "")
            status = (
                old_status
                if old_status in {"confirmed", "rejected", "suppressed"}
                else (
                    "stable"
                    if len(evidence) >= 3 and len(days) >= 2
                    else "recent"
                )
            )
            age = max(
                0,
                (
                    projection_day
                    - datetime.strptime(days[-1], "%Y-%m-%d").date()
                ).days,
            )
            confidence = min(
                1.0, 0.25 + len(evidence) * 0.15 + len(days) * 0.1
            )
            if age > 90:
                confidence *= max(0.2, 1 - (age - 90) / 365)
            correction = _safe_text(user_fact.get("correction"), 500)
            entry_type = min(
                (
                    str(item.get("entry_type") or "model_observation")
                    for item in evidence
                ),
                key=lambda item: type_rank.get(item, 99),
            )
            expected[key] = {
                "key": key,
                "value": value,
                "effective_value": correction or value,
                "entry_type": entry_type,
                "evidence_count": len(evidence),
                "distinct_days": len(days),
                "confidence": round(confidence, 3),
                "first_seen": days[0],
                "last_seen": days[-1],
                "evidence_ids": [
                    str(item.get("contribution_id")) for item in evidence[-30:]
                ],
                "subjects": sorted(
                    {
                        subject
                        for source in sources
                        for subject in _safe_list(source.get("subjects"))
                    }
                ),
                "chapters": sorted(
                    {
                        chapter
                        for source in sources
                        for chapter in _safe_list(source.get("chapters"))
                    }
                ),
                "status": status,
                **({"correction": correction} if correction else {}),
                **(
                    {"user_updated_at": user_fact.get("user_updated_at")}
                    if user_fact.get("user_updated_at")
                    else {}
                ),
            }
        for key, user_fact in authority.items():
            if key in expected:
                continue
            expected[key] = {
                **user_fact,
                "entry_type": user_fact.get("entry_type")
                or "model_observation",
                "effective_value": user_fact.get("effective_value")
                or user_fact.get("correction")
                or user_fact.get("value")
                or "",
            }
        actual: dict[str, dict[str, Any]] = {}
        for item in profile.get("entries") or []:
            key = _safe_text(item.get("key"), 500)
            if not key or key in actual:
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )
            actual[key] = item
        if set(actual) != set(expected):
            raise MemoryAuthorityError("ai_memory_projection_inventory_drift")
        for key, expected_item in expected.items():
            if set(actual[key]) != set(expected_item) or any(
                actual[key].get(field) != value
                for field, value in expected_item.items()
            ):
                raise MemoryAuthorityError(
                    "ai_memory_projection_inventory_drift"
                )

    def _event_contributions(
        self, memory_type: str, event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        operation_id = _safe_text(event.get("operation_id"), 160)
        exam_id = self._event_exam_id(event)
        scope = operation_id or exam_id or str(event.get("event_id") or "")
        values = []
        for entry_type, kind, value in self._typed_evidence(event):
            identity = "|".join(
                (memory_type, scope, entry_type, kind, value.casefold())
            )
            values.append({
                "contribution_id": hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest(),
                "operation_id": operation_id or None,
                "entry_type": entry_type,
                "kind": kind,
                "value": value,
                "fact_key": f"{kind}:{value}",
                "sources": [self._contribution_source(event)],
            })
        if exam_id and operation_id:
            marker_identity = "|".join(
                (memory_type, operation_id, "exam_scope", exam_id)
            )
            values.append({
                "contribution_id": hashlib.sha256(
                    marker_identity.encode("utf-8")
                ).hexdigest(),
                "operation_id": operation_id,
                "entry_type": "model_observation",
                "kind": "__exam_scope__",
                "value": exam_id,
                "fact_key": f"__exam_scope__:{exam_id}",
                "sources": [self._contribution_source(event)],
            })
        return values

    def _refresh_contributions(
        self, memory_type: str, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        existing = self._load_contributions(memory_type)
        live_event_ids = {str(event.get("event_id") or "") for event in events}
        merged: dict[str, dict[str, Any]] = {}
        for raw in existing:
            item = dict(raw)
            sources = [
                source
                for source in self._normalise_contribution_sources(item)
                if str(source.get("event_id") or "") not in live_event_ids
            ]
            if not sources:
                continue
            item.pop("event_id", None)
            item.pop("exam_id", None)
            item.pop("date", None)
            item.pop("actor", None)
            item.pop("subjects", None)
            item.pop("chapters", None)
            item["sources"] = sources
            merged[str(item.get("contribution_id"))] = item
        for event in events:
            for item in self._event_contributions(memory_type, event):
                contribution_id = item["contribution_id"]
                target = merged.setdefault(
                    contribution_id,
                    {key: value for key, value in item.items() if key != "sources"},
                )
                sources = {
                    str(source.get("event_id") or ""): source
                    for source in self._normalise_contribution_sources(target)
                }
                for source in item["sources"]:
                    sources[str(source.get("event_id") or "")] = source
                target["sources"] = [sources[key] for key in sorted(sources)]
        values = sorted(
            merged.values(),
            key=lambda item: (
                str(item.get("date") or ""),
                str(item.get("contribution_id") or ""),
            ),
        )
        _atomic_json(
            self._contribution_path(memory_type),
            {"schema_version": PROFILE_SCHEMA_VERSION, "entries": values},
        )
        return values

    def _active_contributions(
        self,
        memory_type: str,
        contributions: list[dict[str, Any]],
        *,
        strict: bool = False,
        suppressed_exam_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        suppressed = (
            suppressed_exam_ids
            if suppressed_exam_ids is not None
            else self._suppressed_exam_ids(memory_type, strict=strict)
        )
        operation_exams: dict[str, set[str]] = {}
        for item in contributions:
            if item.get("kind") != "__exam_scope__":
                continue
            operation = _safe_text(item.get("operation_id"), 160)
            exam_id = _safe_text(item.get("value"), 160)
            if operation and exam_id:
                operation_exams.setdefault(operation, set()).add(exam_id)
        fully_suppressed_operations = {
            operation
            for operation, exam_ids in operation_exams.items()
            if exam_ids and exam_ids.issubset(suppressed)
        }
        active = []
        for raw in contributions:
            if str(raw.get("kind") or "").startswith("__"):
                continue
            operation = _safe_text(raw.get("operation_id"), 160)
            sources = []
            for source in self._normalise_contribution_sources(raw):
                exam_id = _safe_text(source.get("exam_id"), 160)
                if exam_id and exam_id in suppressed:
                    continue
                if not exam_id and operation in fully_suppressed_operations:
                    continue
                sources.append(source)
            if sources:
                item = dict(raw)
                item["sources"] = sources
                active.append(item)
        return active

    def _actor_scoped_profile_entries(
        self,
        memory_type: str,
        actor: str,
        fallback: list[dict[str, Any]],
        *,
        projected_actors: dict[str, str] | None = None,
        contributions: list[dict[str, Any]] | None = None,
        profile_authority: dict[str, dict[str, Any]] | None = None,
        suppressed_exam_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Project preference facts only from actors authorized for this reader."""

        allowed = PREFERENCE_READ_ACTORS.get((memory_type, actor))
        if allowed is None:
            return fallback
        if projected_actors is None:
            projected_actors = self._legacy_actor_projection(memory_type)
        if contributions is None:
            contributions = self._load_contributions(memory_type, strict=True)
        contributions = self._active_contributions(
            memory_type,
            contributions,
            strict=True,
            suppressed_exam_ids=suppressed_exam_ids,
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for raw in contributions:
            sources = [
                source
                for source in self._normalise_contribution_sources(raw)
                if (
                    _safe_text(source.get("actor"), 80)
                    or projected_actors.get(str(source.get("event_id") or ""), "")
                ) in allowed
            ]
            if not sources:
                continue
            item = dict(raw)
            item["sources"] = sources
            grouped.setdefault(
                (
                    _safe_text(item.get("kind"), 160),
                    _safe_text(item.get("value"), 500),
                ),
                [],
            ).append(item)
        authority = (
            profile_authority
            if profile_authority is not None
            else self._load_profile_authority(memory_type)
        )
        today = _local_now().date()
        type_rank = {
            "explicit_preference": 0,
            "inferred_weakness": 1,
            "mastery_signal": 2,
            "model_observation": 3,
        }
        entries: list[dict[str, Any]] = []
        for (kind, value), evidence in sorted(grouped.items()):
            if not kind or not value:
                continue
            key = f"{kind}:{value}"
            sources = [
                source
                for item in evidence
                for source in self._normalise_contribution_sources(item)
            ]
            day_values = set()
            for source in sources:
                try:
                    day_values.add(_date_string(_safe_text(source.get("date"), 10)))
                except ValueError:
                    continue
            days = sorted(day_values)
            if not days:
                continue
            user_fact = authority.get(key, {})
            old_status = str(user_fact.get("status") or "")
            status = (
                old_status
                if old_status in {"confirmed", "rejected", "suppressed"}
                else ("stable" if len(evidence) >= 3 and len(days) >= 2 else "recent")
            )
            age = max(
                0,
                (
                    today
                    - datetime.strptime(days[-1], "%Y-%m-%d").date()
                ).days,
            )
            confidence = min(1.0, 0.25 + len(evidence) * 0.15 + len(days) * 0.1)
            if age > 90:
                confidence *= max(0.2, 1 - (age - 90) / 365)
            correction = _safe_text(user_fact.get("correction"), 500)
            entry_type = min(
                (
                    str(item.get("entry_type") or "model_observation")
                    for item in evidence
                ),
                key=lambda value: type_rank.get(value, 99),
            )
            entries.append({
                "key": key,
                "value": value,
                "effective_value": correction or value,
                "entry_type": entry_type,
                "evidence_count": len(evidence),
                "distinct_days": len(days),
                "confidence": round(confidence, 3),
                "first_seen": days[0],
                "last_seen": days[-1],
                "evidence_ids": [
                    str(item.get("contribution_id")) for item in evidence[-30:]
                ],
                "subjects": sorted({
                    value
                    for source in sources
                    for value in _safe_list(source.get("subjects"))
                }),
                "chapters": sorted({
                    value
                    for source in sources
                    for value in _safe_list(source.get("chapters"))
                }),
                "status": status,
                **({"correction": correction} if correction else {}),
            })
        return entries

    def _fully_suppressed_operations(
        self, memory_type: str, *, strict: bool = False
    ) -> set[str]:
        contributions = self._load_contributions(memory_type, strict=strict)
        suppressed = self._suppressed_exam_ids(memory_type, strict=strict)
        operation_exams: dict[str, set[str]] = {}
        for item in contributions:
            if item.get("kind") != "__exam_scope__":
                continue
            operation = _safe_text(item.get("operation_id"), 160)
            exam_id = _safe_text(item.get("value"), 160)
            if operation and exam_id:
                operation_exams.setdefault(operation, set()).add(exam_id)
        return {
            operation
            for operation, exam_ids in operation_exams.items()
            if exam_ids and exam_ids.issubset(suppressed)
        }

    def _rebuild_daily_summary(self, memory_type: str, day: str) -> dict[str, Any]:
        """Refresh only the touched day's projection on the append hot path."""
        items = self._read_jsonl(self._event_path(memory_type, day))
        counts: dict[str, dict[str, int]] = {}
        for item in items:
            for key, value in self._evidence(item):
                values = counts.setdefault(key, {})
                values[value] = values.get(value, 0) + 1
        summary = {
            "schema_version": SCHEMA_VERSION,
            "date": day,
            "event_count": len(items),
            "evidence_ids": [str(item.get("event_id")) for item in items],
            "facts": counts,
        }
        _atomic_json(
            self._namespace(memory_type) / "summaries" / "daily" / f"{day}.json",
            summary,
        )
        self._summary_cache.pop(
            self._namespace(memory_type) / "summaries" / "daily" / f"{day}.json",
            None,
        )
        return summary

    def _update_daily_summary(
        self,
        memory_type: str,
        day: str,
        event: dict[str, Any],
        *,
        expected_event_count: int,
    ) -> dict[str, Any]:
        """Increment the touched summary without rereading the daily event log."""
        path = self._namespace(memory_type) / "summaries" / "daily" / f"{day}.json"
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._summary_cache.get(path)
            if cached is not None and cached[:2] == signature:
                summary = cached[2]
            else:
                summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._rebuild_daily_summary(memory_type, day)
        if int(summary.get("event_count") or 0) != expected_event_count - 1:
            return self._rebuild_daily_summary(memory_type, day)
        summary["event_count"] = int(summary.get("event_count") or 0) + 1
        summary.setdefault("evidence_ids", []).append(str(event.get("event_id")))
        facts = summary.setdefault("facts", {})
        for key, value in self._evidence(event):
            values = facts.setdefault(key, {})
            values[value] = int(values.get(value) or 0) + 1
        # This file is a rebuildable projection; the JSONL event is already fsynced.
        _atomic_json(path, summary, durable=False)
        stat = path.stat()
        self._summary_cache[path] = (stat.st_mtime_ns, stat.st_size, summary)
        return summary

    def _rebuild_namespace(self, memory_type: str) -> dict[str, Any]:
        namespace = self._namespace(memory_type)
        events = self._all_events(memory_type)
        by_day: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_day.setdefault(_safe_text(event.get("date"), 10), []).append(event)
        for day in by_day:
            self._rebuild_daily_summary(memory_type, day)
        existing_profile = {}
        profile_path = namespace / "profile.json"
        if profile_path.exists():
            try:
                existing_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing_profile = {}
        authority = self._load_profile_authority(memory_type, existing_profile)
        ledger = self._refresh_contributions(memory_type, events)
        contributions = self._active_contributions(memory_type, ledger)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for contribution in contributions:
            entry_type = str(contribution.get("entry_type") or "model_observation")
            if entry_type not in PROFILE_ENTRY_TYPES:
                entry_type = "model_observation"
            grouped.setdefault((
                str(contribution.get("kind") or "observation"),
                str(contribution.get("value") or ""),
            ), []).append(contribution)
        entries = []
        today = _local_now().date()
        type_rank = {
            "explicit_preference": 0,
            "inferred_weakness": 1,
            "mastery_signal": 2,
            "model_observation": 3,
        }
        for (kind, value), raw_evidence in sorted(grouped.items()):
            key = f"{kind}:{value}"
            evidence_by_scope: dict[str, dict[str, Any]] = {}
            for item in raw_evidence:
                item_sources = self._normalise_contribution_sources(item)
                scope = (
                    _safe_text(item.get("operation_id"), 160)
                    or (
                        _safe_text(item_sources[0].get("event_id"), 160)
                        if item_sources else ""
                    )
                ) or str(item.get("contribution_id") or "")
                current = evidence_by_scope.get(scope)
                if current is None:
                    evidence_by_scope[scope] = dict(item)
                    continue
                current_sources = {
                    str(source.get("event_id") or ""): source
                    for source in self._normalise_contribution_sources(current)
                }
                current_sources.update({
                    str(source.get("event_id") or ""): source
                    for source in self._normalise_contribution_sources(item)
                })
                current["sources"] = [current_sources[source_id] for source_id in sorted(current_sources)]
                if type_rank.get(str(item.get("entry_type") or ""), 99) < type_rank.get(str(current.get("entry_type") or ""), 99):
                    current["entry_type"] = item.get("entry_type")
            evidence = list(evidence_by_scope.values())
            entry_type = min(
                (str(item.get("entry_type") or "model_observation") for item in evidence),
                key=lambda value: type_rank.get(value, 99),
            )
            sources = [
                source
                for item in evidence
                for source in self._normalise_contribution_sources(item)
            ]
            days = []
            for source in sources:
                day_value = _safe_text(source.get("date"), 10)
                try:
                    parsed_day = _date_string(day_value)
                except ValueError:
                    continue
                days.append(parsed_day)
            days = sorted(set(days))
            if not days:
                continue
            user_fact = authority.get(key, {})
            age = max(0, (today - datetime.strptime(days[-1], "%Y-%m-%d").date()).days)
            confidence = min(1.0, 0.25 + len(evidence) * 0.15 + len(days) * 0.1)
            if age > 90:
                confidence *= max(0.2, 1 - (age - 90) / 365)
            old_status = str(user_fact.get("status") or "")
            status = (
                old_status
                if old_status in {"confirmed", "rejected", "suppressed"}
                else ("stable" if len(evidence) >= 3 and len(days) >= 2 else "recent")
            )
            correction = _safe_text(user_fact.get("correction"), 500)
            entries.append({
                "key": key, "value": value, "effective_value": correction or value,
                "entry_type": entry_type,
                "evidence_count": len(evidence), "distinct_days": len(days),
                "confidence": round(confidence, 3), "first_seen": days[0], "last_seen": days[-1],
                "evidence_ids": [str(item.get("contribution_id")) for item in evidence[-30:]],
                "subjects": sorted({value for item in sources for value in _safe_list(item.get("subjects"))}),
                "chapters": sorted({value for item in sources for value in _safe_list(item.get("chapters"))}),
                "status": status,
                **({"correction": correction} if correction else {}),
                **({"user_updated_at": user_fact.get("user_updated_at")} if user_fact.get("user_updated_at") else {}),
            })
        for key, user_fact in authority.items():
            if any(item["key"] == key for item in entries):
                continue
            retained = dict(user_fact)
            retained.setdefault("entry_type", "model_observation")
            retained.setdefault("effective_value", retained.get("correction") or retained.get("value") or "")
            entries.append(retained)
        profile = {"schema_version": PROFILE_SCHEMA_VERSION, "updated_at": _local_now().isoformat(timespec="seconds"), "entries": entries}
        _atomic_json(profile_path, profile)
        confirmed = [item for item in entries if item.get("status") == "confirmed"]
        stable = [item for item in entries if item.get("status") == "stable"]
        lines = ["# 长期记忆", "", "由证据画像自动投影；用户确认项优先。", ""]
        for title, values in (("已确认事实", confirmed), ("稳定偏好", stable)):
            lines.append(f"## {title}")
            for item in values[:40]:
                kind = str(item.get("key") or "画像").split(":", 1)[0]
                effective = _safe_text(
                    item.get("effective_value") or item.get("correction") or item.get("value"),
                    500,
                )
                lines.append(
                    f"- {kind}:{effective}（证据 {int(item.get('evidence_count') or 0)}，"
                    f"置信度 {float(item.get('confidence') or 0.0):.2f}）"
                )
            if not values:
                lines.append("- 暂无")
        archived_facts: dict[str, dict[str, int]] = {}
        for contribution in contributions:
            sources = self._normalise_contribution_sources(contribution)
            if not sources or any(source.get("event_id") in {event.get("event_id") for event in events} for source in sources):
                continue
            kind = str(contribution.get("kind") or "")
            value = str(contribution.get("value") or "")
            if kind and value and f"{kind}:{value}" not in authority:
                target = archived_facts.setdefault(kind, {})
                target[value] = target.get(value, 0) + 1
        lines.append("## 已归档历史概览")
        for kind, values in archived_facts.items():
            rendered = "、".join(f"{value}({count})" for value, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:20])
            lines.append(f"- {kind}：{rendered}")
        if not archived_facts:
            lines.append("- 暂无")
        _atomic_text(namespace / "long_term.md", "\n".join(lines) + "\n")
        manifest = self._manifest(memory_type)
        manifest.pop("profile_stale", None)
        manifest["projection_hashes"] = {
            "version": "ai-memory-projection-hashes-v1",
            "profile": _sha256(profile_path),
            "contributions": _sha256(self._contribution_path(memory_type)),
            "profile_authority": _sha256(
                self._profile_authority_path(memory_type)
            ),
            "suppression": (
                _sha256(self._suppression_path(memory_type))
                if self._suppression_path(memory_type).exists()
                else None
            ),
        }
        manifest.update(
            {
                "schema_version": SCHEMA_VERSION,
                "event_count": len(events),
                "last_event_id": str(events[-1].get("event_id") or "") if events else None,
                "projection_revision": int(manifest.get("projection_revision") or 0) + 1,
                "updated_at": _local_now().isoformat(timespec="seconds"),
            }
        )
        _atomic_json(namespace / "manifest.json", manifest, durable=False)
        return {"events": len(events), "profile_entries": len(entries)}

    def read_context(self, memory_type: str, actor: str, filters: dict[str, Any] | None = None, **kwargs) -> str:
        result = self.read_context_attested(
            memory_type, actor, filters, **kwargs
        )
        return str(result["context"])

    def read_exam_inputs_snapshot(
        self,
        memory_type: str,
        actor: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project every exam read from one stable namespace inventory."""

        if memory_type not in MEMORY_TYPES:
            raise ValueError("未知记忆类型")
        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return {
                "context": "",
                "recent_question_ids": [],
                "recent_question_fingerprints": [],
            }
        namespace = self.root / memory_type
        if not namespace.is_dir():
            raise MemoryAuthorityError("ai_memory_namespace_unavailable")
        try:
            with _file_lock(namespace / ".memory.lock"):
                attestation = self._attest_namespace_read_only(memory_type)
                attested = self._read_context_from_attestation(
                    memory_type,
                    actor,
                    dict(filters or {}),
                    attestation,
                )
                return {
                    "context": str(attested["context"]),
                    "recent_question_ids": sorted(
                        self._recent_question_ids_from_events(
                            memory_type, actor, attestation["events"]
                        )
                    ),
                    "recent_question_fingerprints": sorted(
                        self._recent_question_fingerprints_from_events(
                            memory_type, actor, attestation["events"]
                        )
                    ),
                }
        except OSError as exc:
            raise MemoryAuthorityError("ai_memory_snapshot_unavailable") from exc

    def read_context_attested(
        self,
        memory_type: str,
        actor: str,
        filters: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return {
                "context": "",
                "context_hash": hashlib.sha256(b"").hexdigest(),
                "inventory_hash": hashlib.sha256(
                    _canonical(
                        {
                            "memory_type": memory_type,
                            "actor": actor,
                            "read_authorized": False,
                        }
                    ).encode("utf-8")
                ).hexdigest(),
                "profile_authority_hash": hashlib.sha256(
                    _canonical({"state": "not_read"}).encode("utf-8")
                ).hexdigest(),
            }
        filters = {**(filters or {}), **kwargs}
        return self._read_context_from_attestation(
            memory_type,
            actor,
            filters,
            self._attest_namespace_read_only(memory_type),
        )

    def _read_context_from_attestation(
        self,
        memory_type: str,
        actor: str,
        filters: dict[str, Any],
        attestation: dict[str, Any],
    ) -> dict[str, Any]:
        profile = attestation["profile"]
        today = _local_now().date()
        first_day = (today - timedelta(days=self.daily_days - 1)).isoformat()
        last_day = today.isoformat()
        events = [
            event
            for event in attestation["events"]
            if first_day <= _safe_text(event.get("date"), 10) <= last_day
        ]
        preference_actors = PREFERENCE_READ_ACTORS.get((memory_type, actor))
        if preference_actors is not None:
            projected_actors = attestation["projected_actors"]
            events = [
                event
                for event in events
                if (
                    _safe_text(event.get("actor"), 80)
                    or projected_actors.get(str(event.get("event_id") or ""), "")
                ) in preference_actors
            ]
        subject = _safe_text(filters.get("subject"), 160)
        chapters = set(_safe_list(filters.get("chapters")))
        difficulty = _safe_text(filters.get("difficulty"), 160)
        candidates = set(_safe_list(filters.get("candidates")))

        def relevant(event: dict[str, Any]) -> bool:
            event_subjects = set(_safe_list(event.get("subjects"))) or {_safe_text(event.get("subject"), 160)}
            if subject and event_subjects and subject not in event_subjects:
                return False
            if chapters and event.get("chapters") and not chapters.intersection(event.get("chapters") or []):
                return False
            if difficulty and (event.get("request") or {}).get("difficulty") not in (None, "", difficulty):
                return False
            return True

        entries = self._actor_scoped_profile_entries(
            memory_type,
            actor,
            list(profile.get("entries") or []),
            projected_actors=attestation["projected_actors"],
            contributions=attestation["contributions"],
            profile_authority=attestation["profile_authority"],
            suppressed_exam_ids=attestation["suppressed_exam_ids"],
        )
        profile_status = {
            str(item.get("key") or ""): str(item.get("status") or "")
            for item in entries
            if isinstance(item, dict)
        }

        def recent_priority(event: dict[str, Any]) -> tuple[int, str, str, str, int]:
            fact_keys = [
                f"{kind}:{value}"
                for _, kind, value in self._typed_evidence(event)
            ]
            # The recent layer must not be consumed first by raw events whose
            # facts are already represented in confirmed/stable projections.
            has_recent_fact = not fact_keys or any(
                profile_status.get(key) in {None, "", "recent"}
                for key in fact_keys
            )
            return (
                1 if has_recent_fact else 0,
                str(event.get("date") or ""),
                str(event.get("occurred_at") or ""),
                str(event.get("event_id") or ""),
                len(fact_keys),
            )

        ranked = sorted(
            (item for item in events if relevant(item)),
            key=recent_priority,
            reverse=True,
        )

        def relevant_entry(item: dict[str, Any]) -> bool:
            if item.get("status") in {"rejected", "suppressed"}:
                return False
            entry_subjects = set(_safe_list(item.get("subjects")))
            entry_chapters = set(_safe_list(item.get("chapters")))
            kind, _, raw_value = str(item.get("key") or "").partition(":")
            effective = _safe_text(item.get("effective_value") or item.get("correction") or item.get("value"), 500)
            if subject:
                if kind == "subject":
                    if subject not in {raw_value, effective} and subject not in entry_subjects:
                        return False
                elif entry_subjects and subject not in entry_subjects:
                    return False
            if chapters:
                if kind == "chapter":
                    if (
                        raw_value not in chapters
                        and effective not in chapters
                        and not chapters.intersection(entry_chapters)
                    ):
                        return False
                elif entry_chapters and not chapters.intersection(entry_chapters):
                    return False
            return bool(raw_value or effective)

        confirmed = [
            item for item in entries
            if item.get("status") == "confirmed" and relevant_entry(item)
        ]
        stable = [
            item for item in entries
            if item.get("status") == "stable" and relevant_entry(item)
        ]
        intro = [
            "【历史记忆，仅作选题参考；分析观察不等同于事实】",
            f"【来源：{memory_type} / 投影版本 {PROFILE_SCHEMA_VERSION}】",
        ]
        def entry_line(item: dict[str, Any]) -> str:
            kind = str(item.get("key") or "画像").split(":", 1)[0]
            value = _safe_text(
                item.get("effective_value")
                or item.get("correction")
                or item.get("value"),
                500,
            )
            return (
                f"- [{item.get('entry_type') or 'model_observation'}] "
                f"{kind}:{value}，置信度 {item.get('confidence')}"
            )

        confirmed_lines = [entry_line(item) for item in confirmed]
        stable_lines = [entry_line(item) for item in stable]

        def bounded_lines(values: list[str], budget: int) -> list[str]:
            result = []
            used = 0
            for value in values:
                extra = len(value) + (1 if result else 0)
                if used + extra > budget:
                    break
                result.append(value)
                used += extra
            return result

        overridden = {
            str(item.get("key") or "")
            for item in entries
            if item.get("status") in {"rejected", "suppressed"}
            or bool(item.get("correction"))
        }
        recent_lines = []
        for event in ranked[:40]:
            compact = {key: event.get(key) for key in ("event_id", "date", "event_kind", "subject", "chapters", "request", "observation", "result", "review") if event.get(key) not in (None, "", [], {})}
            if f"subject:{compact.get('subject')}" in overridden:
                compact.pop("subject", None)
            if isinstance(compact.get("chapters"), list):
                compact["chapters"] = [
                    value for value in compact["chapters"]
                    if f"chapter:{value}" not in overridden
                ]
                if not compact["chapters"]:
                    compact.pop("chapters", None)
            if isinstance(compact.get("request"), dict):
                request_value = dict(compact["request"])
                if f"difficulty:{request_value.get('difficulty')}" in overridden:
                    request_value.pop("difficulty", None)
                request_value["types"] = [
                    value for value in request_value.get("types") or []
                    if f"question_type:{value}" not in overridden
                ]
                if not request_value.get("types"):
                    request_value.pop("types", None)
                compact["request"] = request_value
                if not request_value:
                    compact.pop("request", None)
            for result_key in ("observation", "result"):
                if isinstance(compact.get(result_key), dict):
                    result_value = dict(compact[result_key])
                    for field in ("key_concept", "trap"):
                        if f"{field}:{result_value.get(field)}" in overridden:
                            result_value.pop(field, None)
                    result_value["reasons"] = [
                        value for value in result_value.get("reasons") or []
                        if f"reason:{value}" not in overridden
                    ]
                    if not result_value.get("reasons"):
                        result_value.pop("reasons", None)
                    compact[result_key] = result_value
                    if not result_value:
                        compact.pop(result_key, None)
            if isinstance(compact.get("review"), dict):
                review_value = dict(compact["review"])
                for field, fact_kind in (
                    ("result", "review_result"),
                    ("stage", "review_stage"),
                    ("lapse_count", "lapse_count"),
                ):
                    if f"{fact_kind}:{review_value.get(field)}" in overridden:
                        review_value.pop(field, None)
                review_value["reasons"] = [
                    value for value in review_value.get("reasons") or []
                    if f"reason:{value}" not in overridden
                ]
                if not review_value.get("reasons"):
                    review_value.pop("reasons", None)
                compact["review"] = review_value
                if not review_value:
                    compact.pop("review", None)
            recent_lines.append(_canonical(compact))

        intro_chars = sum(len(line) + 1 for line in intro)
        memory_budget = max(0, self.max_context_chars - intro_chars)
        layer_budgets = {
            "confirmed": int(memory_budget * 0.35),
            "stable": int(memory_budget * 0.30),
        }
        layer_budgets["recent"] = memory_budget - layer_budgets["confirmed"] - layer_budgets["stable"]

        def section(title: str, values: list[str], budget: int) -> list[str]:
            if budget <= len(title):
                return []
            selected = bounded_lines(values, budget - len(title) - 1)
            if not selected and len(title) + 4 <= budget:
                selected = ["- 暂无"]
            return [title, *selected]

        lines = [
            *intro,
            *section("【用户确认事实】", confirmed_lines, layer_budgets["confirmed"]),
            *section("【稳定画像】", stable_lines, layer_budgets["stable"]),
            *section("【近期相关事件】", recent_lines, layer_budgets["recent"]),
        ]
        if candidates:
            candidate_lines = ["【本次候选题 ID（与记忆证据分离）】", "、".join(sorted(candidates)[:100])]
            candidate_size = sum(len(line) + 1 for line in candidate_lines)
            if sum(len(line) + 1 for line in lines) + candidate_size <= self.max_context_chars:
                lines.extend(candidate_lines)
        # Add complete records only; never slice a JSON object in half.
        output: list[str] = []
        used = 0
        for line in lines:
            extra = len(line) + (1 if output else 0)
            if used + extra > self.max_context_chars:
                break
            output.append(line)
            used += extra
        context = "\n".join(output)
        return {
            "context": context,
            "context_hash": hashlib.sha256(context.encode("utf-8")).hexdigest(),
            "inventory_hash": attestation["inventory_hash"],
            "profile_authority_hash": attestation["profile_authority_hash"],
        }

    def recent_question_ids(self, memory_type: str, actor: str) -> set[str]:
        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return set()
        if memory_type == "mistakes":
            return set()
        events = self._attest_namespace_read_only(memory_type)["events"]
        return self._recent_question_ids_from_events(memory_type, actor, events)

    def _recent_question_ids_from_events(
        self, memory_type: str, actor: str, events: Iterable[dict[str, Any]]
    ) -> set[str]:
        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return set()
        if memory_type == "mistakes":
            # Mistake memory is preference evidence, never a dedupe source.
            return set()
        today = _local_now().date()
        first_day = (today - timedelta(days=self.daily_days - 1)).isoformat()
        last_day = today.isoformat()
        recent_events = [
            event
            for event in events
            if first_day <= _safe_text(event.get("date"), 10) <= last_day
        ]
        found = set()
        for event in recent_events:
            for question in event.get("questions") or []:
                if not isinstance(question, dict):
                    continue
                found.update(_safe_list(question.get("reference_ids")))
                for key in ("candidate_id", "question_id"):
                    if question.get(key):
                        found.add(str(question[key]))
        return found

    def recent_question_fingerprints(
        self, memory_type: str, actor: str
    ) -> set[str]:
        """Return body-free fingerprints from recent result events."""

        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return set()
        if memory_type == "mistakes":
            return set()
        events = self._attest_namespace_read_only(memory_type)["events"]
        return self._recent_question_fingerprints_from_events(
            memory_type, actor, events
        )

    def _recent_question_fingerprints_from_events(
        self, memory_type: str, actor: str, events: Iterable[dict[str, Any]]
    ) -> set[str]:

        if memory_type not in READ_ROLES.get(actor, frozenset()):
            return set()
        if memory_type == "mistakes":
            # Mistake memory is preference evidence, never a dedupe source.
            return set()
        today = _local_now().date()
        first_day = (today - timedelta(days=self.daily_days - 1)).isoformat()
        last_day = today.isoformat()
        recent_events = [
            event
            for event in events
            if first_day <= _safe_text(event.get("date"), 10) <= last_day
        ]
        found = set()
        for event in recent_events:
            for question in event.get("questions") or []:
                if not isinstance(question, dict):
                    continue
                value = question.get("fingerprint")
                if isinstance(value, str) and value.strip():
                    found.add(value.strip())
        return found

    def rollover_expired_days(self, memory_type: str, *, summarizer: Callable[[str, list[dict[str, Any]]], str] | None = None, today: date | str | None = None) -> list[str]:
        current = datetime.strptime(_date_string(today), "%Y-%m-%d").date()
        cutoff = current - timedelta(days=self.daily_days - 1)
        namespace = self._namespace(memory_type)
        archived = []
        with _file_lock(namespace / ".memory.lock"):
            for path in sorted((namespace / "events" / "daily").glob("*.jsonl")):
                try:
                    file_day = datetime.strptime(path.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_day >= cutoff:
                    continue
                archived.append(path.stem)
                path.unlink(missing_ok=True)
                self._compat_path(memory_type, path.stem).unlink(missing_ok=True)
            if archived:
                self._rebuild_namespace(memory_type)
        return archived

    def status(self) -> dict[str, Any]:
        result = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "event_schema_version": SCHEMA_VERSION,
            "namespaces": [],
        }
        for memory_type in sorted(MEMORY_TYPES):
            namespace = self._namespace(memory_type)
            manifest = self._manifest(memory_type)
            profile = {}
            try:
                profile = json.loads((namespace / "profile.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            entries = profile.get("entries") if isinstance(profile, dict) else []
            manifest_count = manifest.get("event_count")
            event_count = (
                int(manifest_count)
                if isinstance(manifest_count, int) and not manifest.get("profile_stale")
                else len(self._all_events(memory_type))
            )
            result["namespaces"].append({
                "memory_type": memory_type, "paused": bool(manifest.get("paused")),
                "event_count": event_count, "profile_count": len(entries or []),
                "confirmed_count": sum(1 for item in (entries or []) if item.get("status") == "confirmed"),
                "suppressed_exam_count": len(self._suppressed_exam_ids(memory_type)),
                "projection_version": PROFILE_SCHEMA_VERSION,
                "profile_updated_at": profile.get("updated_at") if isinstance(profile, dict) else None,
                "profile_stale": bool(manifest.get("profile_stale")) or (not bool(profile.get("updated_at")) if isinstance(profile, dict) else True),
            })
        result["pending_cleanup_count"] = sum(
            len(self._suppressed_exam_ids(memory_type)) for memory_type in MEMORY_TYPES
        )
        return result

    def set_paused(self, memory_type: str, paused: bool) -> dict[str, Any]:
        namespace = self._namespace(memory_type)
        with _file_lock(namespace / ".memory.lock"):
            manifest = self._manifest(memory_type)
            manifest["paused"] = bool(paused)
            manifest["updated_at"] = _local_now().isoformat(timespec="seconds")
            _atomic_json(namespace / "manifest.json", manifest)
        return {"memory_type": memory_type, "paused": bool(paused)}

    def suppress_exam(self, exam_id: int | str, memory_types: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        """Persist a read-time suppression marker before destructive cleanup."""
        value = str(exam_id)
        targets = tuple(memory_types or sorted(MEMORY_TYPES))
        for memory_type in targets:
            if memory_type not in MEMORY_TYPES:
                continue
            namespace = self._namespace(memory_type)
            with _file_lock(namespace / ".memory.lock"):
                ids = self._suppressed_exam_ids(memory_type)
                ids.add(value)
                _atomic_json(self._suppression_path(memory_type), {"schema_version": SCHEMA_VERSION, "exam_ids": sorted(ids)})
                # Suppression is the crash-safe read barrier. Refresh while the
                # marker is held so stable and operation-level facts disappear
                # before physical compaction starts.
                self._rebuild_namespace(memory_type)
        return {"exam_id": value, "memory_types": list(targets), "suppressed": True}

    def unsuppress_exam(self, exam_id: int | str, memory_types: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        value = str(exam_id)
        targets = tuple(memory_types or sorted(MEMORY_TYPES))
        for memory_type in targets:
            if memory_type not in MEMORY_TYPES:
                continue
            namespace = self._namespace(memory_type)
            with _file_lock(namespace / ".memory.lock"):
                ids = self._suppressed_exam_ids(memory_type)
                ids.discard(value)
                _atomic_json(self._suppression_path(memory_type), {"schema_version": SCHEMA_VERSION, "exam_ids": sorted(ids)})
                self._rebuild_namespace(memory_type)
        return {"exam_id": value, "memory_types": list(targets), "suppressed": False}

    def cleanup_exam(self, exam_id: int | str, memory_types: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        """Idempotently remove result events for one exam and orphaned requests."""
        value = str(exam_id)
        targets = tuple(memory_types or sorted(MEMORY_TYPES))
        removed = 0
        removed_requests = 0
        for memory_type in targets:
            if memory_type not in MEMORY_TYPES:
                continue
            namespace = self._namespace(memory_type)
            with _file_lock(namespace / ".memory.lock"):
                operation_ids: set[str] = set()
                contributions = self._load_contributions(memory_type)
                operation_ids.update(
                    str(item.get("operation_id"))
                    for item in contributions
                    if (
                        str(item.get("exam_id") or "") == value
                        or (
                            item.get("kind") == "__exam_scope__"
                            and str(item.get("value") or "") == value
                        )
                    )
                    and item.get("operation_id")
                )
                for folder in (namespace / "events" / "daily", namespace / "daily"):
                    for path in sorted(folder.glob("*.jsonl")):
                        kept: list[str] = []
                        changed = False
                        for line in path.read_text(encoding="utf-8").splitlines(True) if path.exists() else []:
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                kept.append(line)
                                continue
                            event_exam = self._event_exam_id(event) if isinstance(event, dict) else None
                            if event_exam == value:
                                removed += 1
                                changed = True
                                operation = event.get("operation_id") if isinstance(event, dict) else None
                                if operation:
                                    operation_ids.add(str(operation))
                                continue
                            kept.append(line)
                        if changed:
                            if kept:
                                temp = path.with_suffix(path.suffix + ".tmp")
                                temp.write_text("".join(kept), encoding="utf-8", newline="\n")
                                os.replace(temp, path)
                            else:
                                path.unlink(missing_ok=True)
                # A shared request is retained while any sibling result remains.
                if operation_ids:
                    remaining_ops = {
                        str(event.get("operation_id"))
                        for event in self._raw_events(memory_type)
                        if (event.get("event_kind") or event.get("event_type")) == "result" and event.get("operation_id")
                    }
                    for folder in (namespace / "events" / "daily", namespace / "daily"):
                        for path in sorted(folder.glob("*.jsonl")):
                            kept = []
                            changed = False
                            for line in path.read_text(encoding="utf-8").splitlines(True):
                                try:
                                    event = json.loads(line)
                                except json.JSONDecodeError:
                                    kept.append(line)
                                    continue
                                if (event.get("event_kind") or event.get("event_type")) == "request" and str(event.get("operation_id")) in operation_ids and str(event.get("operation_id")) not in remaining_ops:
                                    removed_requests += 1
                                    changed = True
                                    continue
                                kept.append(line)
                            if changed:
                                if kept:
                                    temp = path.with_suffix(path.suffix + ".tmp")
                                    temp.write_text("".join(kept), encoding="utf-8", newline="\n")
                                    os.replace(temp, path)
                                else:
                                    path.unlink(missing_ok=True)
                retained_contributions = []
                for raw in contributions:
                    item = dict(raw)
                    if (
                        item.get("kind") == "__exam_scope__"
                        and str(item.get("value") or "") == value
                    ):
                        continue
                    sources = [
                        source
                        for source in self._normalise_contribution_sources(item)
                        if str(source.get("exam_id") or "") != value
                    ]
                    if not sources:
                        continue
                    item["sources"] = sources
                    retained_contributions.append(item)
                remaining_operations = {
                    str(item.get("operation_id"))
                    for item in retained_contributions
                    if item.get("kind") == "__exam_scope__"
                    and item.get("operation_id")
                }
                compacted_contributions = []
                for raw in retained_contributions:
                    item = dict(raw)
                    operation = str(item.get("operation_id") or "")
                    sources = self._normalise_contribution_sources(item)
                    if operation in operation_ids and operation not in remaining_operations:
                        sources = [
                            source for source in sources
                            if source.get("exam_id") not in (None, "")
                        ]
                    if sources:
                        item["sources"] = sources
                        compacted_contributions.append(item)
                _atomic_json(
                    self._contribution_path(memory_type),
                    {
                        "schema_version": PROFILE_SCHEMA_VERSION,
                        "entries": compacted_contributions,
                    },
                )
                self._rebuild_namespace(memory_type)
        self.unsuppress_exam(value, targets)
        return {"exam_id": value, "removed_events": removed, "removed_requests": removed_requests, "applied": True}

    def _raw_events(self, memory_type: str) -> list[dict[str, Any]]:
        namespace = self._namespace(memory_type)
        events: dict[str, dict[str, Any]] = {}
        for folder in (namespace / "daily", namespace / "events" / "daily"):
            for path in sorted(folder.glob("*.jsonl")):
                for raw in self._read_jsonl(path):
                    events[str(raw.get("event_id") or _event_id(raw))] = raw
        return list(events.values())

    def clear(self, memory_type: str) -> None:
        namespace = self._namespace(memory_type)
        with _file_lock(namespace / ".memory.lock"):
            for child in list(namespace.iterdir()):
                if child.name == ".memory.lock":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
        self._namespace(memory_type)

    def rebuild(self, memory_type: str) -> dict[str, Any]:
        namespace = self._namespace(memory_type)
        with _file_lock(namespace / ".memory.lock"):
            return self._rebuild_namespace(memory_type)

    def list_profile(
        self,
        memory_type: str,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str = "",
        entry_type: str = "",
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        if status and status not in PROFILE_STATUSES:
            raise ValueError("画像状态无效")
        if entry_type and entry_type not in PROFILE_ENTRY_TYPES:
            raise ValueError("画像类型无效")
        namespace = self._namespace(memory_type)
        with _file_lock(namespace / ".memory.lock"):
            self._rebuild_namespace(memory_type)
            try:
                profile = json.loads(
                    (namespace / "profile.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                profile = {"entries": []}
        entries = [
            item for item in profile.get("entries") or []
            if isinstance(item, dict)
            and (not status or item.get("status") == status)
            and (not entry_type or item.get("entry_type") == entry_type)
        ]
        entries.sort(key=lambda item: (
            0 if item.get("status") == "confirmed" else 1,
            -float(item.get("confidence") or 0.0),
            str(item.get("key") or ""),
        ))
        total = len(entries)
        start = (page - 1) * page_size
        public_entries = []
        for item in entries[start:start + page_size]:
            public_entries.append({
                key: item.get(key)
                for key in (
                    "key", "value", "effective_value", "entry_type", "status",
                    "correction", "confidence", "evidence_count", "distinct_days",
                    "first_seen", "last_seen", "subjects", "chapters",
                )
                if item.get(key) not in (None, "")
            })
        return {
            "memory_type": memory_type,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_next": start + page_size < total,
            "items": public_entries,
        }

    def command_profile(
        self,
        memory_type: str,
        key: str,
        *,
        action: str,
        value: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"confirm", "correct", "reject", "suppress"}:
            raise ValueError("画像操作无效")
        namespace = self._namespace(memory_type)
        with _file_lock(namespace / ".memory.lock"):
            self._rebuild_namespace(memory_type)
            path = namespace / "profile.json"
            profile = json.loads(path.read_text(encoding="utf-8"))
            entry = next(
                (item for item in profile.get("entries", []) if item.get("key") == key),
                None,
            )
            if entry is None:
                raise KeyError("未找到画像条目")
            if action == "confirm":
                entry["status"] = "confirmed"
            elif action == "correct":
                correction = _safe_text(value, 500)
                if not correction:
                    raise ValueError("纠正值不能为空")
                entry["correction"] = correction
                entry["effective_value"] = correction
                entry["status"] = "confirmed"
            else:
                entry["status"] = "rejected" if action == "reject" else "suppressed"
            entry["user_updated_at"] = _local_now().isoformat(timespec="seconds")
            authority = self._load_profile_authority(memory_type, profile)
            authority[key] = {
                key_name: entry.get(key_name)
                for key_name in (
                    "key", "value", "effective_value", "entry_type", "status",
                    "correction", "confidence", "evidence_count", "distinct_days",
                    "first_seen", "last_seen", "subjects", "chapters",
                    "user_updated_at",
                )
                if entry.get(key_name) not in (None, "")
            }
            # Persist the user command outside the rebuildable profile before
            # regenerating any projections.
            self._write_profile_authority(memory_type, authority)
            self._rebuild_namespace(memory_type)
            refreshed = json.loads(path.read_text(encoding="utf-8"))
            return next(
                item for item in refreshed.get("entries", [])
                if item.get("key") == key
            )

    def update_profile(self, memory_type: str, key: str, *, confirmed: bool | None = None, correction: str | None = None) -> dict[str, Any]:
        if correction is not None:
            return self.command_profile(
                memory_type, key, action="correct", value=correction
            )
        if confirmed is None:
            raise ValueError("画像操作不能为空")
        return self.command_profile(
            memory_type,
            key,
            action="confirm" if confirmed else "reject",
        )


def memory_from_app(app) -> AIMemory:
    config = app.config
    return AIMemory(config["AI_MEMORY_DIR"], daily_days=config.get("AI_MEMORY_DAILY_DAYS", 30), max_event_chars=config.get("AI_MEMORY_MAX_EVENT_CHARS", 20_000), max_context_chars=config.get("AI_MEMORY_MAX_CONTEXT_CHARS", 12_000))
