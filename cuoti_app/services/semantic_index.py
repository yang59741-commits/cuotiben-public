"""Deterministic contracts for the permanent semantic question index.

This module deliberately has no database, Flask, worker, or provider side
effects.  Stage A uses it as the single source of truth for identities,
fingerprints, card validation, state transitions, and token-budget arithmetic
before any migration or background job is introduced.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


CONTENT_HASH_SCHEMA_VERSION = 1
METADATA_HASH_SCHEMA_VERSION = 1
SEMANTIC_CARD_SCHEMA_VERSION = 1

FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT = 3_000_000
FIRST_FULL_BUILD_HARD_TOKEN_LIMIT = 5_000_000
TOKEN_EXTENSION_START = 100_000
TOKEN_EXTENSION_STEP = 100_000
TOKEN_EXTENSION_CAP = 600_000
TOKEN_LIMIT_LADDER = (
    3_000_000,
    3_100_000,
    3_300_000,
    3_600_000,
    4_000_000,
    4_500_000,
    5_000_000,
)

CARD_LIST_FIELDS = (
    "knowledge_points",
    "methods",
    "characteristics",
    "prerequisites",
    "common_traps",
    "semantic_keywords",
    "source_constraints",
)
CARD_TEXT_FIELDS = (
    "question_type",
    "difficulty",
    "discrimination",
    "similarity_group",
)
CARD_REQUIRED_FIELDS = frozenset(
    {"candidate_id", "analysis_confidence", *CARD_LIST_FIELDS, *CARD_TEXT_FIELDS}
)
CARD_ALLOWED_FIELDS = CARD_REQUIRED_FIELDS
CARD_LIST_MAX_ITEMS = 32
CARD_LIST_ITEM_MAX_CHARS = 160
CARD_LIST_TOTAL_MAX_CHARS = 2_048
CARD_TEXT_MAX_CHARS = 160
ALLOWED_DIFFICULTIES = frozenset({"简单", "中等", "困难", "未知"})
ALLOWED_DISCRIMINATIONS = frozenset(
    {"低", "较低", "中", "中等", "较高", "高", "未知"}
)


class SemanticIndexContractError(ValueError):
    """Raised when data violates the versioned semantic-index contract."""


class InvalidSemanticIndexTransition(SemanticIndexContractError):
    """Raised when a run or item attempts an illegal state transition."""


class TokenBudgetError(SemanticIndexContractError):
    """Raised when token accounting would become inconsistent or exceed a limit."""


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class SemanticRunStatus(_StringEnum):
    SCANNING = "scanning"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_BUDGET_REVIEW = "paused_budget_review"
    PAUSED_TOKEN_HARD_LIMIT = "paused_token_hard_limit"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SemanticRunItemStatus(_StringEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class SemanticSnapshotStatus(_StringEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"


class SemanticCardStatus(_StringEnum):
    VALID = "valid"
    SUPERSEDED = "superseded"
    INVALID = "invalid"


class SemanticProviderAttemptStatus(_StringEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RESPONSE_VALID = "response_valid"
    RESPONSE_INVALID = "response_invalid"
    PROVIDER_FAILED = "provider_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ABANDONED = "abandoned"
    CHECKPOINTED = "checkpointed"


class SemanticChangeKind(_StringEnum):
    NEW = "new"
    CONTENT_CHANGED = "content_changed"
    METADATA_CHANGED = "metadata_changed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    EXCLUDED = "excluded"
    ANALYSIS_STALE = "analysis_stale"
    RETRY_PENDING = "retry_pending"


RUN_TERMINAL_STATUSES = frozenset(
    {
        SemanticRunStatus.SUCCEEDED,
        SemanticRunStatus.FAILED,
        SemanticRunStatus.CANCELLED,
    }
)
RUN_ALLOWED_TRANSITIONS = {
    SemanticRunStatus.SCANNING: frozenset(
        {
            SemanticRunStatus.QUEUED,
            SemanticRunStatus.FAILED,
            SemanticRunStatus.CANCELLED,
        }
    ),
    SemanticRunStatus.QUEUED: frozenset(
        {SemanticRunStatus.RUNNING, SemanticRunStatus.CANCELLED}
    ),
    SemanticRunStatus.RUNNING: frozenset(
        {
            SemanticRunStatus.PAUSED,
            SemanticRunStatus.PAUSED_BUDGET_REVIEW,
            SemanticRunStatus.PAUSED_TOKEN_HARD_LIMIT,
            SemanticRunStatus.SUCCEEDED,
            SemanticRunStatus.FAILED,
            SemanticRunStatus.CANCELLED,
        }
    ),
    SemanticRunStatus.PAUSED: frozenset(
        {SemanticRunStatus.QUEUED, SemanticRunStatus.CANCELLED}
    ),
    SemanticRunStatus.PAUSED_BUDGET_REVIEW: frozenset(
        {
            SemanticRunStatus.QUEUED,
            SemanticRunStatus.FAILED,
            SemanticRunStatus.CANCELLED,
        }
    ),
    # The 5M hard stop cannot be resumed through the normal state machine.
    SemanticRunStatus.PAUSED_TOKEN_HARD_LIMIT: frozenset(
        {SemanticRunStatus.CANCELLED}
    ),
    SemanticRunStatus.SUCCEEDED: frozenset(),
    SemanticRunStatus.FAILED: frozenset(),
    SemanticRunStatus.CANCELLED: frozenset(),
}

ITEM_TERMINAL_STATUSES = frozenset(
    {
        SemanticRunItemStatus.SUCCEEDED,
        SemanticRunItemStatus.FAILED,
        SemanticRunItemStatus.CANCELLED,
        SemanticRunItemStatus.SKIPPED,
    }
)
ITEM_ALLOWED_TRANSITIONS = {
    SemanticRunItemStatus.PENDING: frozenset(
        {
            SemanticRunItemStatus.LEASED,
            SemanticRunItemStatus.CANCELLED,
            SemanticRunItemStatus.SKIPPED,
        }
    ),
    SemanticRunItemStatus.LEASED: frozenset(
        {
            SemanticRunItemStatus.SUCCEEDED,
            SemanticRunItemStatus.RETRY_WAIT,
            SemanticRunItemStatus.FAILED,
            SemanticRunItemStatus.CANCELLED,
            SemanticRunItemStatus.SKIPPED,
        }
    ),
    SemanticRunItemStatus.RETRY_WAIT: frozenset(
        {
            SemanticRunItemStatus.LEASED,
            SemanticRunItemStatus.FAILED,
            SemanticRunItemStatus.CANCELLED,
        }
    ),
    SemanticRunItemStatus.SUCCEEDED: frozenset(),
    SemanticRunItemStatus.FAILED: frozenset(),
    SemanticRunItemStatus.CANCELLED: frozenset(),
    SemanticRunItemStatus.SKIPPED: frozenset(),
}

PROVIDER_ATTEMPT_ALLOWED_TRANSITIONS = {
    SemanticProviderAttemptStatus.RESERVED: frozenset(
        {
            SemanticProviderAttemptStatus.DISPATCHING,
            SemanticProviderAttemptStatus.ABANDONED,
        }
    ),
    SemanticProviderAttemptStatus.DISPATCHING: frozenset(
        {
            SemanticProviderAttemptStatus.RESPONSE_VALID,
            SemanticProviderAttemptStatus.RESPONSE_INVALID,
            SemanticProviderAttemptStatus.PROVIDER_FAILED,
            SemanticProviderAttemptStatus.OUTCOME_UNKNOWN,
        }
    ),
    SemanticProviderAttemptStatus.RESPONSE_VALID: frozenset(
        {SemanticProviderAttemptStatus.CHECKPOINTED}
    ),
    SemanticProviderAttemptStatus.RESPONSE_INVALID: frozenset(),
    SemanticProviderAttemptStatus.PROVIDER_FAILED: frozenset(),
    SemanticProviderAttemptStatus.OUTCOME_UNKNOWN: frozenset(),
    SemanticProviderAttemptStatus.ABANDONED: frozenset(),
    SemanticProviderAttemptStatus.CHECKPOINTED: frozenset(),
}


def _required_text(value: Any, field: str, *, max_chars: int = 512) -> str:
    if not isinstance(value, str):
        raise SemanticIndexContractError(f"{field} 必须是字符串")
    rendered = unicodedata.normalize("NFC", value).strip()
    if not rendered:
        raise SemanticIndexContractError(f"{field} 不能为空")
    if len(rendered) > max_chars:
        raise SemanticIndexContractError(f"{field} 超过 {max_chars} 字符")
    if any(unicodedata.category(char) == "Cc" for char in rendered):
        raise SemanticIndexContractError(f"{field} 包含控制字符")
    return rendered


def _optional_text(value: Any, field: str, *, max_chars: int = 1_024) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise SemanticIndexContractError(f"{field} 必须是字符串、整数或空值")
    rendered = unicodedata.normalize("NFC", str(value)).strip()
    if len(rendered) > max_chars:
        raise SemanticIndexContractError(f"{field} 超过 {max_chars} 字符")
    return rendered


def _utf8_text(value: str | bytes, field: str) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise SemanticIndexContractError(f"{field} 不是有效 UTF-8") from exc
    if not isinstance(value, str):
        raise SemanticIndexContractError(f"{field} 必须是字符串或 UTF-8 字节")
    return value


def normalize_semantic_content(value: str | bytes, *, field: str = "content") -> str:
    """Normalize only irrelevant encoding/line-tail differences.

    Internal spaces and all LaTeX control characters remain intact because
    collapsing them can change mathematical meaning.
    """

    text = unicodedata.normalize("NFC", _utf8_text(value, field))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _normalize_content_sequence(
    values: Iterable[str | bytes] | None, field: str
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise SemanticIndexContractError(f"{field} 必须是字符串序列")
    return tuple(
        normalize_semantic_content(value, field=f"{field}[{index}]")
        for index, value in enumerate(values)
    )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticQuestionIdentity:
    source_kind: str
    candidate_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_kind",
            _required_text(self.source_kind, "source_kind", max_chars=64).casefold(),
        )
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, "candidate_id", max_chars=256),
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_kind, self.candidate_id)


def semantic_content_hash(
    content: str | bytes,
    *,
    figure_references: Iterable[str | bytes] | None = None,
    options: Iterable[str | bytes] | None = None,
    conditions: Iterable[str | bytes] | None = None,
) -> str:
    payload = {
        "schema_version": CONTENT_HASH_SCHEMA_VERSION,
        "content": normalize_semantic_content(content),
        "figure_references": _normalize_content_sequence(
            figure_references, "figure_references"
        ),
        "options": _normalize_content_sequence(options, "options"),
        "conditions": _normalize_content_sequence(conditions, "conditions"),
    }
    if not payload["content"]:
        raise SemanticIndexContractError("content 不能为空")
    return _sha256_payload(payload)


def semantic_metadata_hash(
    *,
    subject: str,
    chapter: str,
    source_file: str | None = None,
    source_year: int | None = None,
    source_number: str | int | None = None,
    source_note: str | None = None,
    display_order: int | None = None,
    active: bool = True,
    excluded: bool = False,
) -> str:
    if source_year is not None and (
        isinstance(source_year, bool) or not isinstance(source_year, int)
    ):
        raise SemanticIndexContractError("source_year 必须是整数或空值")
    if display_order is not None and (
        isinstance(display_order, bool) or not isinstance(display_order, int)
    ):
        raise SemanticIndexContractError("display_order 必须是整数或空值")
    if not isinstance(active, bool) or not isinstance(excluded, bool):
        raise SemanticIndexContractError("active 和 excluded 必须是布尔值")
    payload = {
        "schema_version": METADATA_HASH_SCHEMA_VERSION,
        "subject": _required_text(subject, "subject", max_chars=160),
        "chapter": _required_text(chapter, "chapter", max_chars=320),
        "source_file": _optional_text(source_file, "source_file", max_chars=2_048),
        "source_year": source_year,
        "source_number": _optional_text(source_number, "source_number"),
        "source_note": _optional_text(source_note, "source_note", max_chars=2_048),
        "display_order": display_order,
        "active": active,
        "excluded": excluded,
    }
    return _sha256_payload(payload)


def _card_text(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise SemanticIndexContractError(f"材料卡字段 {field} 必须是字符串")
    rendered = " ".join(unicodedata.normalize("NFC", value).split())
    if required and not rendered:
        raise SemanticIndexContractError(f"材料卡字段 {field} 不能为空")
    if len(rendered) > CARD_TEXT_MAX_CHARS:
        raise SemanticIndexContractError(
            f"材料卡字段 {field} 超过 {CARD_TEXT_MAX_CHARS} 字符"
        )
    return rendered


def _card_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SemanticIndexContractError(f"材料卡字段 {field} 必须是数组")
    if len(value) > CARD_LIST_MAX_ITEMS:
        raise SemanticIndexContractError(
            f"材料卡字段 {field} 超过 {CARD_LIST_MAX_ITEMS} 项"
        )
    rendered: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for index, raw in enumerate(value):
        if not isinstance(raw, str):
            raise SemanticIndexContractError(
                f"材料卡字段 {field}[{index}] 必须是字符串"
            )
        item = " ".join(unicodedata.normalize("NFC", raw).split())
        if not item:
            continue
        if len(item) > CARD_LIST_ITEM_MAX_CHARS:
            raise SemanticIndexContractError(
                f"材料卡字段 {field}[{index}] 超过 {CARD_LIST_ITEM_MAX_CHARS} 字符"
            )
        dedupe_key = item.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rendered.append(item)
        total_chars += len(item)
    if total_chars > CARD_LIST_TOTAL_MAX_CHARS:
        raise SemanticIndexContractError(
            f"材料卡字段 {field} 总长度超过 {CARD_LIST_TOTAL_MAX_CHARS} 字符"
        )
    return tuple(rendered)


@dataclass(frozen=True, slots=True)
class ValidatedSemanticCard:
    candidate_id: str
    knowledge_points: tuple[str, ...]
    question_type: str
    methods: tuple[str, ...]
    difficulty: str
    discrimination: str
    similarity_group: str
    characteristics: tuple[str, ...]
    prerequisites: tuple[str, ...]
    common_traps: tuple[str, ...]
    semantic_keywords: tuple[str, ...]
    source_constraints: tuple[str, ...]
    analysis_confidence: float
    ignored_field_count: int = 0

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("ignored_field_count")
        for field in CARD_LIST_FIELDS:
            payload[field] = list(payload[field])
        return payload

    @property
    def card_hash(self) -> str:
        return _sha256_payload(
            {
                "schema_version": SEMANTIC_CARD_SCHEMA_VERSION,
                "card": self.as_payload(),
            }
        )


def validate_semantic_card(
    raw: Mapping[str, Any], *, expected_candidate_id: str | None = None
) -> ValidatedSemanticCard:
    if not isinstance(raw, Mapping):
        raise SemanticIndexContractError("材料卡必须是对象")
    missing = sorted(CARD_REQUIRED_FIELDS - set(raw))
    if missing:
        raise SemanticIndexContractError(
            "材料卡缺少字段：" + ",".join(missing)
        )
    candidate_id = _required_text(raw["candidate_id"], "candidate_id", max_chars=256)
    if expected_candidate_id is not None:
        expected = _required_text(
            expected_candidate_id, "expected_candidate_id", max_chars=256
        )
        if candidate_id != expected:
            raise SemanticIndexContractError("材料卡 candidate_id 与冻结题目不一致")

    difficulty = _card_text(raw["difficulty"], "difficulty", required=True)
    if difficulty not in ALLOWED_DIFFICULTIES:
        raise SemanticIndexContractError("材料卡 difficulty 不在允许枚举中")
    discrimination = _card_text(
        raw["discrimination"], "discrimination", required=True
    )
    if discrimination not in ALLOWED_DISCRIMINATIONS:
        raise SemanticIndexContractError("材料卡 discrimination 不在允许枚举中")
    confidence = raw["analysis_confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SemanticIndexContractError("材料卡 analysis_confidence 必须是数字")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise SemanticIndexContractError("材料卡 analysis_confidence 必须在 0 到 1 之间")

    return ValidatedSemanticCard(
        candidate_id=candidate_id,
        knowledge_points=_card_list(raw["knowledge_points"], "knowledge_points"),
        question_type=_card_text(raw["question_type"], "question_type", required=True),
        methods=_card_list(raw["methods"], "methods"),
        difficulty=difficulty,
        discrimination=discrimination,
        similarity_group=_card_text(raw["similarity_group"], "similarity_group"),
        characteristics=_card_list(raw["characteristics"], "characteristics"),
        prerequisites=_card_list(raw["prerequisites"], "prerequisites"),
        common_traps=_card_list(raw["common_traps"], "common_traps"),
        semantic_keywords=_card_list(raw["semantic_keywords"], "semantic_keywords"),
        source_constraints=_card_list(raw["source_constraints"], "source_constraints"),
        analysis_confidence=confidence,
        ignored_field_count=len(set(raw) - CARD_ALLOWED_FIELDS),
    )


def _enum_value(value: Any, enum_type: type[_StringEnum], field: str) -> _StringEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise SemanticIndexContractError(f"{field} 状态无效：{value}") from exc


def assert_run_transition(current: Any, target: Any) -> None:
    current_status = _enum_value(current, SemanticRunStatus, "run current")
    target_status = _enum_value(target, SemanticRunStatus, "run target")
    if target_status not in RUN_ALLOWED_TRANSITIONS[current_status]:
        raise InvalidSemanticIndexTransition(
            f"run 不允许从 {current_status.value} 变为 {target_status.value}"
        )


def assert_run_item_transition(current: Any, target: Any) -> None:
    current_status = _enum_value(current, SemanticRunItemStatus, "item current")
    target_status = _enum_value(target, SemanticRunItemStatus, "item target")
    if target_status not in ITEM_ALLOWED_TRANSITIONS[current_status]:
        raise InvalidSemanticIndexTransition(
            f"item 不允许从 {current_status.value} 变为 {target_status.value}"
        )


def assert_provider_attempt_transition(current: Any, target: Any) -> None:
    current_status = _enum_value(
        current, SemanticProviderAttemptStatus, "provider attempt current"
    )
    target_status = _enum_value(
        target, SemanticProviderAttemptStatus, "provider attempt target"
    )
    if target_status not in PROVIDER_ATTEMPT_ALLOWED_TRANSITIONS[current_status]:
        raise InvalidSemanticIndexTransition(
            "provider attempt 不允许从 "
            f"{current_status.value} 变为 {target_status.value}"
        )


def assert_snapshot_immutable(current: Any, target: Any) -> None:
    current_status = _enum_value(current, SemanticSnapshotStatus, "snapshot current")
    target_status = _enum_value(target, SemanticSnapshotStatus, "snapshot target")
    if current_status != target_status:
        raise InvalidSemanticIndexTransition(
            "已发布 snapshot 不允许改变状态；应发布新的只读 snapshot"
        )


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenBudgetError(f"{field} 必须是非负整数")
    return value


@dataclass(frozen=True, slots=True)
class TokenBudgetExtension:
    extension_index: int
    previous_limit: int
    nominal_amount: int
    actual_amount: int
    new_limit: int


@dataclass(frozen=True, slots=True)
class FirstFullBuildTokenLedger:
    current_limit: int = FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT
    extension_index: int = 0
    consumed: int = 0
    reserved: int = 0
    consumed_unverified: int = 0

    def __post_init__(self) -> None:
        _nonnegative_integer(self.extension_index, "extension_index")
        _nonnegative_integer(self.consumed, "consumed")
        _nonnegative_integer(self.reserved, "reserved")
        _nonnegative_integer(self.consumed_unverified, "consumed_unverified")
        if self.extension_index >= len(TOKEN_LIMIT_LADDER):
            raise TokenBudgetError("extension_index 超过预算阶梯")
        if self.current_limit != TOKEN_LIMIT_LADDER[self.extension_index]:
            raise TokenBudgetError("current_limit 与 extension_index 不一致")
        if self.consumed_unverified > self.consumed:
            raise TokenBudgetError("未核实消耗不能超过总消耗")
        if self.consumed + self.reserved > self.current_limit:
            raise TokenBudgetError("消耗与预留之和超过当前额度")
        if self.current_limit > FIRST_FULL_BUILD_HARD_TOKEN_LIMIT:
            raise TokenBudgetError("当前额度超过 500 万硬上限")

    @property
    def available(self) -> int:
        return self.current_limit - self.consumed - self.reserved

    @property
    def hard_limit_reached(self) -> bool:
        return (
            self.current_limit == FIRST_FULL_BUILD_HARD_TOKEN_LIMIT
            and self.available == 0
        )

    @property
    def next_nominal_extension(self) -> int | None:
        next_index = self.extension_index + 1
        if next_index >= len(TOKEN_LIMIT_LADDER):
            return None
        return min(
            TOKEN_EXTENSION_START
            + (next_index - 1) * TOKEN_EXTENSION_STEP,
            TOKEN_EXTENSION_CAP,
        )

    def reserve(self, amount: int) -> "FirstFullBuildTokenLedger":
        amount = _nonnegative_integer(amount, "reservation")
        if amount == 0:
            raise TokenBudgetError("reservation 必须大于 0")
        if amount > self.available:
            raise TokenBudgetError("当前额度不足，禁止提交可能越线的请求")
        return replace(self, reserved=self.reserved + amount)

    def settle(
        self, reservation: int, actual_tokens: int | None
    ) -> "FirstFullBuildTokenLedger":
        reservation = _nonnegative_integer(reservation, "reservation")
        if reservation == 0 or reservation > self.reserved:
            raise TokenBudgetError("结算额度未被有效预留")
        if actual_tokens is None:
            charged = reservation
            unverified = self.consumed_unverified + reservation
        else:
            charged = _nonnegative_integer(actual_tokens, "actual_tokens")
            if charged > reservation:
                raise TokenBudgetError("真实消耗超过请求前最坏情况预留")
            unverified = self.consumed_unverified
        return replace(
            self,
            consumed=self.consumed + charged,
            reserved=self.reserved - reservation,
            consumed_unverified=unverified,
        )

    def reconcile_unverified(
        self, reservation: int, actual_tokens: int
    ) -> "FirstFullBuildTokenLedger":
        reservation = _nonnegative_integer(reservation, "reservation")
        actual_tokens = _nonnegative_integer(actual_tokens, "actual_tokens")
        if reservation == 0 or reservation > self.consumed_unverified:
            raise TokenBudgetError("待核实额度不存在或已完成对账")
        if actual_tokens > reservation:
            raise TokenBudgetError("核实消耗超过原保守记账额度")
        return replace(
            self,
            consumed=self.consumed - reservation + actual_tokens,
            consumed_unverified=self.consumed_unverified - reservation,
        )

    def extend(self) -> tuple["FirstFullBuildTokenLedger", TokenBudgetExtension]:
        next_index = self.extension_index + 1
        if next_index >= len(TOKEN_LIMIT_LADDER):
            raise TokenBudgetError("已达到 500 万硬上限，不存在下一次追加")
        nominal = self.next_nominal_extension
        assert nominal is not None
        new_limit = TOKEN_LIMIT_LADDER[next_index]
        event = TokenBudgetExtension(
            extension_index=next_index,
            previous_limit=self.current_limit,
            nominal_amount=nominal,
            actual_amount=new_limit - self.current_limit,
            new_limit=new_limit,
        )
        return replace(
            self, current_limit=new_limit, extension_index=next_index
        ), event


def validate_contract_constants() -> None:
    """Fail fast if future edits make the documented token ladder inconsistent."""

    if TOKEN_LIMIT_LADDER[0] != FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT:
        raise TokenBudgetError("预算阶梯起点不是 300 万")
    if TOKEN_LIMIT_LADDER[-1] != FIRST_FULL_BUILD_HARD_TOKEN_LIMIT:
        raise TokenBudgetError("预算阶梯终点不是 500 万")
    ledger = FirstFullBuildTokenLedger()
    events: list[TokenBudgetExtension] = []
    while ledger.extension_index + 1 < len(TOKEN_LIMIT_LADDER):
        ledger, event = ledger.extend()
        events.append(event)
    if [event.nominal_amount for event in events] != [
        100_000,
        200_000,
        300_000,
        400_000,
        500_000,
        600_000,
    ]:
        raise TokenBudgetError("名义追加序列与计划不一致")
    if [event.actual_amount for event in events] != [
        100_000,
        200_000,
        300_000,
        400_000,
        500_000,
        500_000,
    ]:
        raise TokenBudgetError("实际追加序列与 500 万硬上限不一致")


validate_contract_constants()
