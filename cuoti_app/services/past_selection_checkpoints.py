"""Durable, metadata-only checkpoints for sharded past-exam selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .local_file_lock import file_lock


SELECTION_CHECKPOINT_SCHEMA_VERSION = 2
LEGACY_SELECTION_CHECKPOINT_SCHEMA_VERSION = 1
MAX_SELECTION_CHECKPOINT_BYTES = 1_048_576
MAX_SELECTION_CHECKPOINT_POSITIONS = 4
MAX_SELECTION_CHECKPOINT_CANDIDATES = 16
MAX_SELECTION_PAPER_POSITIONS = 256
MAX_REJECTED_SELECTION_POSITIONS = 256
MAX_REJECTED_CANDIDATES_PER_POSITION = 16

_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "job_hash", "context_hash", "papers"}
)
_LEGACY_TOP_LEVEL_FIELDS = frozenset({"schema_version", "job_hash", "papers"})
_PAPER_FIELDS = frozenset(
    {
        "paper_index",
        "expected_positions",
        "validation_status",
        "selection_hash",
        "rejected_candidate_ids_by_position",
        "shards",
    }
)
_LEGACY_PAPER_FIELDS = frozenset({"paper_index", "shards"})
_SHARD_FIELDS = frozenset(
    {
        "shard_index",
        "positions",
        "candidate_ids",
        "status",
        "effort",
        "request_hash",
    }
)
_SHARD_STATUSES = frozenset(
    {"pending", "running", "complete", "failed", "cancelled"}
)
_VALIDATION_STATUSES = frozenset(
    {"provisional", "selection_validated", "quality_validated"}
)
_REASONING_EFFORTS = frozenset({"low", "medium", "high", "max"})
_REQUIREMENT_FIELDS = frozenset({"source_kind", "chapter"})
_CANDIDATE_FACT_FIELDS = frozenset(
    {
        "source_kind",
        "chapter",
        "source_file",
        "source_number",
        "source_ordinal",
        "source_institution",
        "source_subject",
        "source_year",
        "source_section",
        "content_fingerprint",
    }
)
_REQUIRED_CANDIDATE_FACT_FIELDS = frozenset(
    {"source_kind", "chapter", "source_file", "source_number", "content_fingerprint"}
)
UPSTREAM_PAPER_CHANGED_REASON = "upstream_paper_changed"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SelectionCheckpointError(ValueError):
    code = "selection_checkpoint_conflict"
    retryable = False
    permanent = True


class SelectionCheckpointConflictError(SelectionCheckpointError):
    """Permanent schema, identity, migration, or state conflict."""


class SelectionCheckpointStorageError(SelectionCheckpointError):
    code = "selection_checkpoint_storage"
    retryable = True
    permanent = False


def _conflict(message: str) -> SelectionCheckpointConflictError:
    return SelectionCheckpointConflictError(message)


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: frozenset[str], location: str
) -> None:
    if any(key not in allowed for key in value):
        raise _conflict(f"{location} contains unsupported fields")


def _positive_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _conflict(f"{location} must be a positive integer")
    return value


def _sha256(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise _conflict(f"{location} must be a SHA-256 digest")
    normalized = value.strip().casefold()
    if not _SHA256.fullmatch(normalized):
        raise _conflict(f"{location} must be a SHA-256 digest")
    return normalized


def _identifier(value: Any, location: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _conflict(f"{location} must be text")
    normalized = value.strip()
    if (
        (not normalized and not allow_empty)
        or len(normalized) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise _conflict(f"{location} is invalid")
    return normalized


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_positions(value: Any, location: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or not value:
        raise _conflict(f"{location} must be a non-empty list")
    if len(value) > MAX_SELECTION_PAPER_POSITIONS:
        raise _conflict(f"{location} exceeds the paper limit")
    positions = [_positive_integer(item, location) for item in value]
    if len(positions) != len(set(positions)):
        raise _conflict(f"{location} must be unique")
    return sorted(positions)


def _normalize_rejected_candidates(value: Any, location: str) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        raise _conflict(f"{location} must be an object")
    if len(value) > MAX_REJECTED_SELECTION_POSITIONS:
        raise _conflict(f"{location} exceeds the position limit")
    normalized: dict[str, list[str]] = {}
    for raw_position, raw_ids in value.items():
        try:
            position = int(raw_position)
        except (TypeError, ValueError) as exc:
            raise _conflict(f"{location} contains an invalid position") from exc
        if position < 1 or str(raw_position).strip() != str(position):
            raise _conflict(f"{location} contains an invalid position")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise _conflict(f"{location} candidate lists must be non-empty")
        if len(raw_ids) > MAX_REJECTED_CANDIDATES_PER_POSITION:
            raise _conflict(f"{location} candidate list exceeds the limit")
        candidate_ids = [_identifier(item, location) for item in raw_ids]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise _conflict(f"{location} candidate IDs must be unique")
        normalized[str(position)] = candidate_ids
    return dict(sorted(normalized.items(), key=lambda item: int(item[0])))


def _normalize_shard(
    value: Any, location: str, *, legacy_running_to_pending: bool = False
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _conflict(f"{location} must be an object")
    _reject_unknown_fields(value, _SHARD_FIELDS, location)
    if set(value) != _SHARD_FIELDS:
        raise _conflict(f"{location} is missing required fields")
    raw_positions = value["positions"]
    if not isinstance(raw_positions, list) or not raw_positions:
        raise _conflict(f"{location}.positions must be a non-empty list")
    if len(raw_positions) > MAX_SELECTION_CHECKPOINT_POSITIONS:
        raise _conflict(f"{location}.positions exceeds the shard limit")
    positions = [_positive_integer(item, f"{location}.positions") for item in raw_positions]
    if len(positions) != len(set(positions)):
        raise _conflict(f"{location}.positions must be unique")
    raw_candidate_ids = value["candidate_ids"]
    if not isinstance(raw_candidate_ids, list) or not raw_candidate_ids:
        raise _conflict(f"{location}.candidate_ids must be a non-empty list")
    if len(raw_candidate_ids) > MAX_SELECTION_CHECKPOINT_CANDIDATES:
        raise _conflict(f"{location}.candidate_ids exceeds the shard limit")
    candidate_ids = [_identifier(item, f"{location}.candidate_ids") for item in raw_candidate_ids]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise _conflict(f"{location}.candidate_ids must be unique")
    if len(candidate_ids) != len(positions):
        raise _conflict(f"{location} must pair each position with one candidate")
    raw_status = value["status"]
    status = raw_status.strip().casefold() if isinstance(raw_status, str) else ""
    if status not in _SHARD_STATUSES:
        raise _conflict(f"{location}.status is invalid")
    if legacy_running_to_pending and status == "running":
        status = "pending"
    raw_effort = value["effort"]
    effort = raw_effort.strip().casefold() if isinstance(raw_effort, str) else ""
    if effort not in _REASONING_EFFORTS:
        raise _conflict(f"{location}.effort is invalid")
    return {
        "shard_index": _positive_integer(value["shard_index"], f"{location}.shard_index"),
        "positions": positions,
        "candidate_ids": candidate_ids,
        "status": status,
        "effort": effort,
        "request_hash": _sha256(value["request_hash"], f"{location}.request_hash"),
    }


def selection_mapping_hash(assignments: Mapping[Any, Any]) -> str:
    """Hash one non-empty canonical position-to-candidate assignment."""

    if not isinstance(assignments, Mapping) or not assignments:
        raise _conflict("selection assignments must be a non-empty object")
    selected: dict[int, str] = {}
    for raw_position, raw_candidate_id in assignments.items():
        try:
            position = int(raw_position)
        except (TypeError, ValueError) as exc:
            raise _conflict("selection assignments contain an invalid position") from exc
        if position < 1 or str(raw_position).strip() != str(position):
            raise _conflict("selection assignments contain an invalid position")
        if position in selected:
            raise _conflict("selection assignments contain duplicate positions")
        selected[position] = _identifier(
            raw_candidate_id, "selection assignment candidate_id"
        )
    return _stable_hash(
        [
            {"position": position, "candidate_id": selected[position]}
            for position in sorted(selected)
        ]
    )


def _selection_mapping_hash(paper: Mapping[str, Any]) -> str:
    expected = set(int(value) for value in paper["expected_positions"])
    selected: dict[int, str] = {}
    for shard in paper["shards"]:
        if shard["status"] != "complete":
            raise _conflict("selection checkpoint has incomplete shards")
        for position, candidate_id in zip(shard["positions"], shard["candidate_ids"]):
            position = int(position)
            if position in selected:
                raise _conflict("selection checkpoint shards overlap")
            selected[position] = str(candidate_id)
    if set(selected) != expected:
        raise _conflict("selection checkpoint positions are incomplete")
    return selection_mapping_hash(selected)


def normalize_selection_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one v2 checkpoint; v1 requires the explicit migration API."""

    if not isinstance(payload, Mapping):
        raise _conflict("selection checkpoint must be an object")
    _reject_unknown_fields(payload, _TOP_LEVEL_FIELDS, "selection checkpoint")
    if set(payload) != _TOP_LEVEL_FIELDS:
        raise _conflict("selection checkpoint is missing required fields")
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        raise _conflict("selection checkpoint schema requires explicit migration")
    papers_raw = payload["papers"]
    if not isinstance(papers_raw, list):
        raise _conflict("selection checkpoint papers must be a list")
    papers: list[dict[str, Any]] = []
    seen_papers: set[int] = set()
    for offset, raw_paper in enumerate(papers_raw):
        location = f"papers[{offset}]"
        if not isinstance(raw_paper, Mapping):
            raise _conflict(f"{location} must be an object")
        _reject_unknown_fields(raw_paper, _PAPER_FIELDS, location)
        if set(raw_paper) != _PAPER_FIELDS:
            raise _conflict(f"{location} is missing required fields")
        paper_index = _positive_integer(raw_paper["paper_index"], f"{location}.paper_index")
        if paper_index in seen_papers:
            raise _conflict("paper_index values must be unique")
        seen_papers.add(paper_index)
        expected_positions = _canonical_positions(raw_paper["expected_positions"], f"{location}.expected_positions")
        expected = set(expected_positions)
        status_raw = raw_paper["validation_status"]
        status = status_raw.strip().casefold() if isinstance(status_raw, str) else ""
        if status not in _VALIDATION_STATUSES:
            raise _conflict(f"{location}.validation_status is invalid")
        raw_selection_hash = raw_paper["selection_hash"]
        selection_hash = None if raw_selection_hash is None else _sha256(raw_selection_hash, f"{location}.selection_hash")
        rejected = _normalize_rejected_candidates(
            raw_paper["rejected_candidate_ids_by_position"],
            f"{location}.rejected_candidate_ids_by_position",
        )
        if any(int(position) not in expected for position in rejected):
            raise _conflict(f"{location} rejected positions are outside the paper")
        shards_raw = raw_paper["shards"]
        if not isinstance(shards_raw, list):
            raise _conflict(f"{location}.shards must be a list")
        shards: list[dict[str, Any]] = []
        shard_indexes: set[int] = set()
        covered: set[int] = set()
        for shard_offset, raw_shard in enumerate(shards_raw):
            shard = _normalize_shard(raw_shard, f"{location}.shards[{shard_offset}]")
            if shard["shard_index"] in shard_indexes:
                raise _conflict(f"{location}.shard_index values must be unique")
            shard_indexes.add(shard["shard_index"])
            positions = set(int(value) for value in shard["positions"])
            if not positions.issubset(expected):
                raise _conflict(f"{location} shard positions are outside the paper")
            if covered.intersection(positions):
                raise _conflict(f"{location} shards overlap")
            covered.update(positions)
            if any(
                str(candidate_id) in rejected.get(str(position), ())
                for position, candidate_id in zip(
                    shard["positions"], shard["candidate_ids"]
                )
            ):
                raise _conflict(f"{location} reuses a rejected candidate")
            shards.append(shard)
        shards.sort(key=lambda item: item["shard_index"])
        paper = {
            "paper_index": paper_index,
            "expected_positions": expected_positions,
            "validation_status": status,
            "selection_hash": selection_hash,
            "rejected_candidate_ids_by_position": rejected,
            "shards": shards,
        }
        if status == "provisional":
            if selection_hash is not None:
                raise _conflict(f"{location} provisional selection hash must be null")
        else:
            calculated = _selection_mapping_hash(paper)
            if selection_hash != calculated:
                raise _conflict(f"{location} selection hash does not match")
        papers.append(paper)
    papers.sort(key=lambda item: item["paper_index"])
    return {
        "schema_version": 2,
        "job_hash": _sha256(payload["job_hash"], "selection checkpoint job_hash"),
        "context_hash": _sha256(payload["context_hash"], "selection checkpoint context_hash"),
        "papers": papers,
    }


def sanitize_selection_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_selection_checkpoint(payload)


def selection_checkpoint_digest(payload: Mapping[str, Any]) -> str:
    return _stable_hash(normalize_selection_checkpoint(payload))


def _canonical_paper_mapping(value: Any, location: str) -> dict[int, Any]:
    if not isinstance(value, Mapping) or not value:
        raise _conflict(f"{location} must be a non-empty object")
    result: dict[int, Any] = {}
    for raw_index, item in value.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise _conflict(f"{location} contains an invalid paper") from exc
        if index < 1 or str(raw_index).strip() != str(index):
            raise _conflict(f"{location} contains an invalid paper")
        if index in result:
            raise _conflict(f"{location} contains duplicate papers")
        result[index] = item
    return result


def _normalize_context(
    expected_positions_by_paper: Mapping[Any, Any],
    frozen_requirements_by_paper: Mapping[Any, Any],
    candidate_facts_by_id: Mapping[Any, Any],
) -> tuple[dict[int, list[int]], dict[int, dict[int, dict[str, str]]], dict[str, dict[str, Any]], str]:
    expected_raw = _canonical_paper_mapping(expected_positions_by_paper, "expected_positions_by_paper")
    requirements_raw = _canonical_paper_mapping(frozen_requirements_by_paper, "frozen_requirements_by_paper")
    if set(expected_raw) != set(requirements_raw):
        raise _conflict("selection checkpoint paper context does not match")
    expected: dict[int, list[int]] = {}
    requirements: dict[int, dict[int, dict[str, str]]] = {}
    for paper_index in sorted(expected_raw):
        expected[paper_index] = _canonical_positions(expected_raw[paper_index], "expected_positions")
        raw_positions = requirements_raw[paper_index]
        if not isinstance(raw_positions, Mapping):
            raise _conflict("frozen requirements must be an object")
        normalized_positions: dict[int, dict[str, str]] = {}
        for raw_position, raw_requirement in raw_positions.items():
            try:
                position = int(raw_position)
            except (TypeError, ValueError) as exc:
                raise _conflict("frozen requirements contain an invalid position") from exc
            if position < 1 or str(raw_position).strip() != str(position):
                raise _conflict("frozen requirements contain an invalid position")
            if not isinstance(raw_requirement, Mapping):
                raise _conflict("frozen requirement must be an object")
            _reject_unknown_fields(raw_requirement, _REQUIREMENT_FIELDS, "frozen requirement")
            if set(raw_requirement) != _REQUIREMENT_FIELDS:
                raise _conflict("frozen requirement is missing required fields")
            source_kind = _identifier(raw_requirement["source_kind"], "source_kind")
            if source_kind not in {"ordinary", "past_exam"}:
                raise _conflict("frozen requirement source kind is invalid")
            normalized_positions[position] = {
                "source_kind": source_kind,
                "chapter": _identifier(raw_requirement["chapter"], "chapter", allow_empty=True),
            }
        if set(normalized_positions) != set(expected[paper_index]):
            raise _conflict("frozen requirements do not cover expected positions")
        requirements[paper_index] = normalized_positions
    if not isinstance(candidate_facts_by_id, Mapping) or not candidate_facts_by_id:
        raise _conflict("candidate facts must be a non-empty object")
    facts: dict[str, dict[str, Any]] = {}
    for raw_candidate_id, raw_fact in candidate_facts_by_id.items():
        candidate_id = _identifier(raw_candidate_id, "candidate_id")
        if candidate_id in facts or not isinstance(raw_fact, Mapping):
            raise _conflict("candidate facts are invalid")
        _reject_unknown_fields(raw_fact, _CANDIDATE_FACT_FIELDS, "candidate fact")
        if not _REQUIRED_CANDIDATE_FACT_FIELDS.issubset(raw_fact):
            raise _conflict("candidate fact is missing required fields")
        fact: dict[str, Any] = {
            "source_kind": _identifier(raw_fact["source_kind"], "source_kind"),
            "chapter": _identifier(raw_fact["chapter"], "chapter", allow_empty=True),
            "source_file": _identifier(raw_fact["source_file"], "source_file"),
            "source_number": _positive_integer(raw_fact["source_number"], "source_number"),
            "content_fingerprint": _sha256(raw_fact["content_fingerprint"], "content_fingerprint"),
        }
        if fact["source_kind"] not in {"ordinary", "past_exam"}:
            raise _conflict("candidate fact source kind is invalid")
        for key in sorted(set(raw_fact).difference(_REQUIRED_CANDIDATE_FACT_FIELDS)):
            value = raw_fact[key]
            fact[key] = (
                _positive_integer(value, key)
                if key in {"source_ordinal", "source_year"}
                else _identifier(value, key, allow_empty=True)
            )
        facts[candidate_id] = fact
    canonical = {
        "expected_positions_by_paper": {str(key): expected[key] for key in sorted(expected)},
        "frozen_requirements_by_paper": {
            str(paper): {str(position): requirements[paper][position] for position in sorted(requirements[paper])}
            for paper in sorted(requirements)
        },
        "candidate_facts_by_id": {key: facts[key] for key in sorted(facts)},
    }
    return expected, requirements, facts, _stable_hash(canonical)


def selection_checkpoint_context_hash(
    *,
    expected_positions_by_paper: Mapping[Any, Any],
    frozen_requirements_by_paper: Mapping[Any, Any],
    candidate_facts_by_id: Mapping[Any, Any],
) -> str:
    return _normalize_context(
        expected_positions_by_paper,
        frozen_requirements_by_paper,
        candidate_facts_by_id,
    )[3]


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _read_raw_unlocked(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_SELECTION_CHECKPOINT_BYTES:
        raise _conflict("selection checkpoint is too large")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _conflict("selection checkpoint cannot be decoded") from exc
    if not isinstance(decoded, Mapping):
        raise _conflict("selection checkpoint must be an object")
    return decoded


def _load_unlocked(path: Path) -> dict[str, Any] | None:
    payload = _read_raw_unlocked(path)
    return None if payload is None else normalize_selection_checkpoint(payload)


def _recover_running_shards(checkpoint: dict[str, Any] | None) -> bool:
    changed = False
    if checkpoint is None:
        return changed
    for paper in checkpoint["papers"]:
        for shard in paper["shards"]:
            if shard["status"] == "running":
                shard["status"] = "pending"
                changed = True
    return changed


def _write_unlocked(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(encoded.encode("utf-8")) > MAX_SELECTION_CHECKPOINT_BYTES:
        raise _conflict("selection checkpoint is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _storage_failure() -> SelectionCheckpointStorageError:
    return SelectionCheckpointStorageError("selection checkpoint storage is temporarily unavailable")


def _locked_update(path: Path, update: Callable[[dict[str, Any] | None], dict[str, Any]]) -> dict[str, Any]:
    try:
        with file_lock(_lock_path(path)):
            checkpoint = _load_unlocked(path)
            _recover_running_shards(checkpoint)
            result = normalize_selection_checkpoint(update(checkpoint))
            _write_unlocked(path, result)
            return result
    except SelectionCheckpointError:
        raise
    except OSError as exc:
        raise _storage_failure() from exc


def _require_identity(checkpoint: Mapping[str, Any] | None, job_hash: str, context_hash: str) -> None:
    if checkpoint is None or checkpoint["job_hash"] != job_hash or checkpoint["context_hash"] != context_hash:
        raise _conflict("selection checkpoint identity does not match")


def _paper(checkpoint: Mapping[str, Any], paper_index: int) -> dict[str, Any]:
    for paper in checkpoint["papers"]:
        if int(paper["paper_index"]) == paper_index:
            return paper
    raise _conflict("selection checkpoint paper does not exist")


def load_selection_checkpoint(path: str | Path) -> dict[str, Any] | None:
    try:
        checkpoint_path = Path(path)
        with file_lock(_lock_path(checkpoint_path)):
            checkpoint = _load_unlocked(checkpoint_path)
            if _recover_running_shards(checkpoint):
                checkpoint = normalize_selection_checkpoint(checkpoint)  # type: ignore[arg-type]
                _write_unlocked(checkpoint_path, checkpoint)
            return checkpoint
    except SelectionCheckpointError:
        raise
    except OSError as exc:
        raise _storage_failure() from exc


def _merge_checkpoint(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    if existing is None:
        if any(paper["validation_status"] != "provisional" for paper in incoming["papers"]):
            raise _conflict("new selection checkpoint must be provisional")
        return dict(incoming)
    _require_identity(existing, str(incoming["job_hash"]), str(incoming["context_hash"]))
    papers = {int(item["paper_index"]): dict(item) for item in existing["papers"]}
    for incoming_paper in incoming["papers"]:
        index = int(incoming_paper["paper_index"])
        current = papers.get(index)
        if current is None:
            if incoming_paper["validation_status"] != "provisional":
                raise _conflict("new selection checkpoint paper must be provisional")
            papers[index] = dict(incoming_paper)
            continue
        if current["expected_positions"] != incoming_paper["expected_positions"]:
            raise _conflict("selection checkpoint expected positions changed")
        if current["validation_status"] != incoming_paper["validation_status"] or current["selection_hash"] != incoming_paper["selection_hash"]:
            raise _conflict("selection checkpoint state changes require a controlled API")
        if current["validation_status"] != "provisional" and current != incoming_paper:
            raise _conflict("validated selection checkpoint data is immutable")
        shards = {int(item["shard_index"]): dict(item) for item in current["shards"]}
        shards.update({int(item["shard_index"]): dict(item) for item in incoming_paper["shards"]})
        current["shards"] = sorted(shards.values(), key=lambda item: int(item["shard_index"]))
        rejected = {str(key): list(value) for key, value in current["rejected_candidate_ids_by_position"].items()}
        for position, ids in incoming_paper["rejected_candidate_ids_by_position"].items():
            rejected[position] = list(dict.fromkeys(rejected.get(position, []) + ids))
        current["rejected_candidate_ids_by_position"] = rejected
    return normalize_selection_checkpoint({
        "schema_version": 2,
        "job_hash": existing["job_hash"],
        "context_hash": existing["context_hash"],
        "papers": sorted(papers.values(), key=lambda item: int(item["paper_index"])),
    })


def write_selection_checkpoint(path: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    incoming = normalize_selection_checkpoint(payload)
    return _locked_update(Path(path), lambda existing: _merge_checkpoint(existing, incoming))


def upsert_selection_checkpoint_shard(
    path: str | Path, *, job_hash: str, context_hash: str, paper_index: int, shard: Mapping[str, Any]
) -> dict[str, Any]:
    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    index = _positive_integer(paper_index, "paper_index")
    normalized_shard = _normalize_shard(shard, "shard")
    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        paper = _paper(checkpoint, index)  # type: ignore[arg-type]
        if paper["validation_status"] != "provisional":
            raise _conflict("validated selection checkpoint data is immutable")
        if any(
            str(candidate_id)
            in paper["rejected_candidate_ids_by_position"].get(str(position), ())
            for position, candidate_id in zip(
                normalized_shard["positions"], normalized_shard["candidate_ids"]
            )
        ):
            raise _conflict("selection checkpoint shard reuses a rejected candidate")
        shards = {int(item["shard_index"]): dict(item) for item in paper["shards"]}
        shards[int(normalized_shard["shard_index"])] = normalized_shard
        paper["shards"] = sorted(shards.values(), key=lambda item: int(item["shard_index"]))
        paper["selection_hash"] = None
        return checkpoint  # type: ignore[return-value]
    return _locked_update(Path(path), update)


def transition_selection_checkpoint_paper(
    path: str | Path,
    *,
    job_hash: str,
    context_hash: str,
    paper_index: int,
    target_status: str,
    reviewed_selection_hash: str | None = None,
) -> dict[str, Any]:
    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    index = _positive_integer(paper_index, "paper_index")
    target = target_status.strip().casefold() if isinstance(target_status, str) else ""
    if target not in {"selection_validated", "quality_validated"}:
        raise _conflict("selection checkpoint validation transition is invalid")
    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        paper = _paper(checkpoint, index)  # type: ignore[arg-type]
        current = paper["validation_status"]
        if target == "selection_validated":
            if current != "provisional" or reviewed_selection_hash is not None:
                raise _conflict("selection checkpoint validation transition is invalid")
            paper["selection_hash"] = _selection_mapping_hash(paper)
        else:
            if current != "selection_validated" or reviewed_selection_hash is None:
                raise _conflict("selection checkpoint validation transition is invalid")
            reviewed = _sha256(reviewed_selection_hash, "reviewed_selection_hash")
            if reviewed != paper["selection_hash"]:
                raise _conflict("reviewed selection identity does not match")
        paper["validation_status"] = target
        return checkpoint  # type: ignore[return-value]
    return _locked_update(Path(path), update)


def downgrade_following_selection_checkpoint_papers(
    path: str | Path, *, job_hash: str, context_hash: str, changed_paper_index: int, reason: str
) -> dict[str, Any]:
    if reason != UPSTREAM_PAPER_CHANGED_REASON:
        raise _conflict("selection checkpoint downgrade reason is invalid")
    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    changed = _positive_integer(changed_paper_index, "changed_paper_index")
    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        for paper in checkpoint["papers"]:  # type: ignore[index]
            if int(paper["paper_index"]) > changed and paper["validation_status"] == "quality_validated":
                paper["validation_status"] = "selection_validated"
        return checkpoint  # type: ignore[return-value]
    return _locked_update(Path(path), update)


def reject_selection_checkpoint_candidates(
    path: str | Path,
    *,
    job_hash: str,
    context_hash: str,
    paper_index: int,
    rejected_candidate_ids_by_position: Mapping[str | int, list[str]],
) -> dict[str, Any]:
    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    index = _positive_integer(paper_index, "paper_index")
    rejected = _normalize_rejected_candidates(rejected_candidate_ids_by_position, "rejected candidates")
    if not rejected:
        raise _conflict("rejected candidate positions must be non-empty")
    affected = {int(value) for value in rejected}
    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        paper = _paper(checkpoint, index)  # type: ignore[arg-type]
        if not affected.issubset(set(paper["expected_positions"])):
            raise _conflict("rejected positions are outside the paper")
        merged = {str(key): list(value) for key, value in paper["rejected_candidate_ids_by_position"].items()}
        for position, ids in rejected.items():
            merged[position] = list(dict.fromkeys(merged.get(position, []) + ids))
            if len(merged[position]) > MAX_REJECTED_CANDIDATES_PER_POSITION:
                raise _conflict("rejected candidate list exceeds the limit")
        paper["rejected_candidate_ids_by_position"] = merged
        paper["shards"] = [shard for shard in paper["shards"] if not affected.intersection(shard["positions"])]
        paper["validation_status"] = "provisional"
        paper["selection_hash"] = None
        return checkpoint  # type: ignore[return-value]
    return _locked_update(Path(path), update)


def clear_selection_checkpoint_paper(
    path: str | Path, *, job_hash: str, context_hash: str, paper_index: int
) -> dict[str, Any]:
    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    index = _positive_integer(paper_index, "paper_index")
    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        paper = _paper(checkpoint, index)  # type: ignore[arg-type]
        paper["shards"] = []
        paper["validation_status"] = "provisional"
        paper["selection_hash"] = None
        return checkpoint  # type: ignore[return-value]
    return _locked_update(Path(path), update)


def replace_validated_selection_checkpoint_candidate(
    path: str | Path,
    *,
    job_hash: str,
    context_hash: str,
    paper_index: int,
    position: int,
    expected_candidate_id: str,
    replacement_candidate_id: str,
    validated_selection_hash: str,
) -> dict[str, Any]:
    """Atomically apply one caller-audited replacement to a validated selection."""

    identity = _sha256(job_hash, "job_hash")
    context = _sha256(context_hash, "context_hash")
    index = _positive_integer(paper_index, "paper_index")
    selected_position = _positive_integer(position, "position")
    expected_id = _identifier(expected_candidate_id, "expected_candidate_id")
    replacement_id = _identifier(
        replacement_candidate_id, "replacement_candidate_id"
    )
    validated_hash = _sha256(
        validated_selection_hash, "validated_selection_hash"
    )

    def update(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        _require_identity(checkpoint, identity, context)
        paper = _paper(checkpoint, index)  # type: ignore[arg-type]
        if paper["validation_status"] != "selection_validated":
            raise _conflict("selection checkpoint paper is not replaceable")
        matches: list[tuple[dict[str, Any], int]] = []
        for shard in paper["shards"]:
            if shard["status"] != "complete":
                raise _conflict("selection checkpoint has incomplete shards")
            for offset, shard_position in enumerate(shard["positions"]):
                if int(shard_position) == selected_position:
                    matches.append((shard, offset))
        if len(matches) != 1:
            raise _conflict("selection checkpoint position is not uniquely covered")
        target_shard, candidate_offset = matches[0]
        if str(target_shard["candidate_ids"][candidate_offset]) != expected_id:
            raise _conflict("selection checkpoint candidate identity does not match")
        if replacement_id in paper["rejected_candidate_ids_by_position"].get(
            str(selected_position), ()
        ):
            raise _conflict("selection checkpoint replacement is rejected")
        target_shard["candidate_ids"][candidate_offset] = replacement_id
        replacement_hash = _selection_mapping_hash(paper)
        if replacement_hash != validated_hash:
            raise _conflict("validated replacement selection identity does not match")
        paper["selection_hash"] = replacement_hash
        return checkpoint  # type: ignore[return-value]

    return _locked_update(Path(path), update)


def _normalize_legacy_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise _conflict("legacy selection checkpoint must be an object")
    _reject_unknown_fields(payload, _LEGACY_TOP_LEVEL_FIELDS, "legacy selection checkpoint")
    version = payload.get("schema_version")
    if (
        set(payload) != _LEGACY_TOP_LEVEL_FIELDS
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
    ):
        raise _conflict("legacy selection checkpoint schema is invalid")
    raw_papers = payload["papers"]
    if not isinstance(raw_papers, list) or not raw_papers:
        raise _conflict("legacy selection checkpoint papers must be a non-empty list")
    papers = []
    seen_papers: set[int] = set()
    for paper_offset, raw_paper in enumerate(raw_papers):
        if not isinstance(raw_paper, Mapping):
            raise _conflict("legacy selection checkpoint paper is invalid")
        _reject_unknown_fields(raw_paper, _LEGACY_PAPER_FIELDS, "legacy paper")
        if set(raw_paper) != _LEGACY_PAPER_FIELDS:
            raise _conflict("legacy paper is missing required fields")
        index = _positive_integer(raw_paper["paper_index"], "paper_index")
        if index in seen_papers:
            raise _conflict("legacy paper indexes overlap")
        seen_papers.add(index)
        if not isinstance(raw_paper["shards"], list):
            raise _conflict("legacy paper shards must be a list")
        shards = [_normalize_shard(item, "legacy shard", legacy_running_to_pending=True) for item in raw_paper["shards"]]
        if len({item["shard_index"] for item in shards}) != len(shards):
            raise _conflict("legacy shard indexes overlap")
        papers.append({"paper_index": index, "shards": shards})
    return {"schema_version": 1, "job_hash": _sha256(payload["job_hash"], "job_hash"), "papers": papers}


def _candidate_matches_requirement(fact: Mapping[str, Any], requirement: Mapping[str, str]) -> bool:
    if fact["source_kind"] != requirement["source_kind"]:
        return False
    return not requirement["chapter"] or fact["chapter"] == requirement["chapter"]


def migrate_selection_checkpoint_v1_to_v2(
    path: str | Path,
    *,
    job_hash: str,
    legacy_context_hash: str,
    expected_positions_by_paper: Mapping[Any, Any],
    frozen_requirements_by_paper: Mapping[Any, Any],
    candidate_facts_by_id: Mapping[Any, Any],
) -> dict[str, Any]:
    """Strictly migrate one v1 file using external frozen identity facts."""

    checkpoint_path = Path(path)
    identity = _sha256(job_hash, "job_hash")
    legacy_identity = _sha256(legacy_context_hash, "legacy_context_hash")
    try:
        with file_lock(_lock_path(checkpoint_path)):
            expected, requirements, facts, context_hash = _normalize_context(
                expected_positions_by_paper,
                frozen_requirements_by_paper,
                candidate_facts_by_id,
            )
            raw = _read_raw_unlocked(checkpoint_path)
            if raw is None:
                raise _conflict("legacy selection checkpoint does not exist")
            legacy = _normalize_legacy_checkpoint(raw)
            legacy_paper_indexes = {
                int(paper["paper_index"]) for paper in legacy["papers"]
            }
            if (
                legacy["job_hash"] != legacy_identity
                or not legacy_paper_indexes
                or not legacy_paper_indexes.issubset(set(expected))
            ):
                raise _conflict("legacy selection checkpoint identity does not match")
            legacy_by_index = {
                int(paper["paper_index"]): paper for paper in legacy["papers"]
            }
            papers: list[dict[str, Any]] = []
            for paper_index in sorted(expected):
                legacy_paper = legacy_by_index.get(paper_index)
                if legacy_paper is None:
                    papers.append({
                        "paper_index": paper_index,
                        "expected_positions": expected[paper_index],
                        "validation_status": "provisional",
                        "selection_hash": None,
                        "rejected_candidate_ids_by_position": {},
                        "shards": [],
                    })
                    continue
                expected_set = set(expected[paper_index])
                covered: set[int] = set()
                migrated_shards = []
                for shard in legacy_paper["shards"]:
                    positions = set(int(value) for value in shard["positions"])
                    if not positions.issubset(expected_set) or covered.intersection(positions):
                        raise _conflict("legacy selection checkpoint positions conflict")
                    covered.update(positions)
                    assignment_matches = True
                    for position, candidate_id in zip(shard["positions"], shard["candidate_ids"]):
                        fact = facts.get(str(candidate_id))
                        if fact is None:
                            raise _conflict("legacy selection checkpoint references an unknown candidate")
                        if not _candidate_matches_requirement(fact, requirements[paper_index][int(position)]):
                            assignment_matches = False
                    migrated = dict(shard)
                    if not assignment_matches:
                        migrated["status"] = "pending"
                    migrated_shards.append(migrated)
                papers.append({
                    "paper_index": paper_index,
                    "expected_positions": expected[paper_index],
                    "validation_status": "provisional",
                    "selection_hash": None,
                    "rejected_candidate_ids_by_position": {},
                    "shards": migrated_shards,
                })
            migrated_checkpoint = normalize_selection_checkpoint({
                "schema_version": 2,
                "job_hash": identity,
                "context_hash": context_hash,
                "papers": papers,
            })
            _write_unlocked(checkpoint_path, migrated_checkpoint)
            return migrated_checkpoint
    except SelectionCheckpointError:
        raise
    except OSError as exc:
        raise _storage_failure() from exc


__all__ = [
    "LEGACY_SELECTION_CHECKPOINT_SCHEMA_VERSION",
    "MAX_REJECTED_CANDIDATES_PER_POSITION",
    "MAX_REJECTED_SELECTION_POSITIONS",
    "MAX_SELECTION_CHECKPOINT_BYTES",
    "MAX_SELECTION_CHECKPOINT_CANDIDATES",
    "MAX_SELECTION_CHECKPOINT_POSITIONS",
    "MAX_SELECTION_PAPER_POSITIONS",
    "SELECTION_CHECKPOINT_SCHEMA_VERSION",
    "SelectionCheckpointConflictError",
    "SelectionCheckpointError",
    "SelectionCheckpointStorageError",
    "UPSTREAM_PAPER_CHANGED_REASON",
    "clear_selection_checkpoint_paper",
    "downgrade_following_selection_checkpoint_papers",
    "load_selection_checkpoint",
    "migrate_selection_checkpoint_v1_to_v2",
    "normalize_selection_checkpoint",
    "reject_selection_checkpoint_candidates",
    "replace_validated_selection_checkpoint_candidate",
    "sanitize_selection_checkpoint",
    "selection_checkpoint_context_hash",
    "selection_checkpoint_digest",
    "selection_mapping_hash",
    "transition_selection_checkpoint_paper",
    "upsert_selection_checkpoint_shard",
    "write_selection_checkpoint",
]
