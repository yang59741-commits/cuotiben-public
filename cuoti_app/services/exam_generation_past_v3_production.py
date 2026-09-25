"""Production bindings for the strict formal-past v3 pipeline.

The binding is intentionally narrow: it derives policy only from the frozen
Stage-3 identity, the configured catalog and the immutable native usage
ledger.  AI callbacks use the already-frozen job provider settings and never
read or change credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ..database import get_db
from .ai_memory import memory_from_app
from functools import partial
from .exam_generation_d2_render_probe import probe_rendering_risk, make_production_compile_callback
from ..migrations.v46_exam_generation_v3_publish import USAGE
from .exam_generation_past_v3_dependencies import (
    PastV3ProductionAuthorities,
    make_past_v3_production_dependencies_factory,
)
from .exam_generation_past_v3_integration import (
    PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION,
    PAST_V3_PUBLICATION_RECEIPT_EXTENSION,
)
from .exam_generation_phase8_authority import (
    AUTHORITY_REGISTRY_VERSION,
    EXACT_HISTORY_SNAPSHOT_VERSION,
)
from .exam_generation_stage4_difficulty import (
    CLASSIFICATION_RESPONSE_VERSION,
    VERIFICATION_RESPONSE_VERSION,
)
from .exam_generation_stage6_scoring import RESPONSE_VERSION as SCORE_RESPONSE_VERSION
from .exam_generation_stage6_rubric import (
    FACTOR_APPLICABILITY_VERSION,
    FACTOR_CATALOG,
    FACTOR_CATALOG_HASH,
    RUBRIC_CATALOG_VERSION,
    RUBRIC_RESPONSE_VERSION,
    WINDOW_RUBRIC_RESPONSE_VERSION,
    CandidateBaseRubricError,
    candidate_base_key,
)
from .exam_generation_stage7_quality import (
    ADAPTATION_PROTOCOL_VERSION,
    QUALITY_PROTOCOL_VERSION,
    VERIFICATION_PROTOCOL_VERSION,
    Stage7Budget,
    formal_original_quality_policy,
)
from .exam_generation_v3_publish import ARTIFACT_VERSION, USAGE_VERSION
from .settings import get_settings_service
from .exam_generation_request import D2_SCORING_PROTOCOL
from .tex import compile_latex_artifact


CHAPTER_POLICY_VERSION = "past-v3-catalog-source-breadth-v1"
DIVERSITY_POLICY_VERSION = "past-v3-source-exam-diversity-max1-v1"
COVERAGE_POLICY_VERSION = "past-v3-explicit-coverage-none-v1"
UNIQUENESS_POLICY_VERSION = "past-v3-candidate-content-uniqueness-v1"
RECENT_USAGE_LIMIT = 256
DIFFICULTY_CLASSIFICATION_PROMPT_VERSION = (
    "past-v3-difficulty-classification-v1"
)
DIFFICULTY_VERIFICATION_PROMPT_VERSION = (
    "past-v3-difficulty-independent-verification-v1"
)
DIFFICULTY_CALLBACK_CHUNK_SIZE = 16
SCORE_PROMPT_VERSION = "past-v3-stage6-score-v5"
RUBRIC_PROMPT_VERSION = "past-v3-stage6-candidate-base-rubric-v1"

_DIFFICULTY_RULES = {
    "version": "past-v3-difficulty-rules-v1",
    "labels": ["简单", "中等", "困难"],
    "confidence_threshold": 0.6,
    "unknown_policy": "return_null_label_and_zero_confidence",
    "decision_scope": "question_statement_only",
}
DIFFICULTY_RULES_HASH = hashlib.sha256(
    json.dumps(
        _DIFFICULTY_RULES,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


class PastV3ProductionBindingError(RuntimeError):
    """A fail-closed production authority or adapter configuration error."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = "exam_past_v3_binding_unavailable"
        self.status_code = 422
        self.reason = reason


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PastV3ProductionBindingError(
            "non_canonical_json", "生产权威数据不是规范 JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_json_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw) > 2_000_000:
        raise PastV3ProductionBindingError(
            "ai_response_not_json_text", "AI 响应不是有界 JSON 文本"
        )

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise PastV3ProductionBindingError(
                    "ai_response_duplicate_key", "AI 响应包含重复 JSON 键"
                )
            value[key] = item
        return value

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except PastV3ProductionBindingError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PastV3ProductionBindingError(
            "ai_response_invalid_json", "AI 响应不是严格 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise PastV3ProductionBindingError(
            "ai_response_not_object", "AI 响应必须是 JSON 对象"
        )
    return value


def _authority_entry(version: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {"version": version, "payload": json.loads(_canonical(payload))}
    return {**body, "authority_hash": _digest(body)}


def _catalog_path_document(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise PastV3ProductionBindingError(
            "catalog_file_missing", "当前目录配置文件不存在"
        )
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PastV3ProductionBindingError(
            "catalog_file_invalid", "当前目录配置文件不可验证"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("subjects"), list)
    ):
        raise PastV3ProductionBindingError(
            "catalog_schema_invalid", "当前目录配置版本无效"
        )
    return document, f"catalog-schema1-sha256-{hashlib.sha256(raw).hexdigest()}"


def _catalog_document(catalog: Any) -> tuple[dict[str, Any], str]:
    return _catalog_path_document(Path(getattr(catalog, "path", "")))


def _catalog_eol_versions(path: Path) -> frozenset[str]:
    """Return only the byte identities reachable by LF/CRLF normalization."""

    try:
        raw = path.read_bytes()
    except OSError:
        return frozenset()
    lf = raw.replace(b"\r\n", b"\n")
    if b"\r" in lf:
        return frozenset()
    crlf = lf.replace(b"\n", b"\r\n")
    return frozenset(
        f"catalog-schema1-sha256-{hashlib.sha256(value).hexdigest()}"
        for value in (lf, crlf)
    )


def build_catalog_authority_registry(
    catalog: Any, *, stage3: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a Stage-5 registry from the exact current catalog identity."""

    if not isinstance(stage3, Mapping):
        raise PastV3ProductionBindingError(
            "stage3_contract_invalid", "阶段 3 合同无效"
        )
    source_identity = stage3.get("source_identity")
    combination = stage3.get("combination")
    if not isinstance(source_identity, Mapping) or not isinstance(combination, Mapping):
        raise PastV3ProductionBindingError(
            "stage3_identity_missing", "阶段 3 来源或画像组合身份缺失"
        )
    subject_name = str(combination.get("subject") or "").strip()
    document, active_catalog_version = _catalog_document(catalog)
    bound_catalog_version = source_identity.get("catalog_version")
    catalog_version = active_catalog_version
    if bound_catalog_version != active_catalog_version:
        default_path = Path(getattr(catalog, "default_path", ""))
        try:
            default_document, default_catalog_version = _catalog_path_document(
                default_path
            )
        except PastV3ProductionBindingError:
            default_document, default_catalog_version = None, None
        # CatalogService may have copied/reformatted an otherwise identical
        # catalog. Accept either the exact configured default document or the
        # current document's exact LF/CRLF byte identity. The course identity
        # is still checked below, so a real chapter/source change fails closed.
        default_copy_matches = (
            bound_catalog_version == default_catalog_version
            and default_document is not None
            and _canonical(document) == _canonical(default_document)
        )
        eol_equivalent = (
            isinstance(bound_catalog_version, str)
            and bound_catalog_version
            in _catalog_eol_versions(Path(getattr(catalog, "path", "")))
        )
        if default_copy_matches or eol_equivalent:
            catalog_version = str(bound_catalog_version)
        else:
            raise PastV3ProductionBindingError(
                "catalog_profile_identity_mismatch",
                "当前目录身份与冻结画像目录身份不一致",
            )
    matching = [
        item
        for item in document["subjects"]
        if isinstance(item, Mapping) and str(item.get("name") or "").strip() == subject_name
    ]
    if len(matching) != 1:
        raise PastV3ProductionBindingError(
            "catalog_subject_missing", "当前目录不含唯一的画像学科"
        )
    raw_subject = matching[0]
    course_version = f"catalog-subject-v1-sha256-{_digest(raw_subject)}"
    if (
        bound_catalog_version != catalog_version
        or source_identity.get("course_version") != course_version
    ):
        raise PastV3ProductionBindingError(
            "catalog_profile_identity_mismatch",
            "当前目录身份与冻结画像目录身份不一致",
        )
    chapters = raw_subject.get("chapters")
    if not isinstance(chapters, list):
        raise PastV3ProductionBindingError(
            "catalog_chapters_invalid", "当前学科章节目录无效"
        )
    weights: dict[str, int] = {}
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise PastV3ProductionBindingError(
                "catalog_chapter_invalid", "当前学科章节条目无效"
            )
        name = str(chapter.get("name") or "").strip()
        sources = chapter.get("sources")
        if not name or not isinstance(sources, list):
            raise PastV3ProductionBindingError(
                "catalog_chapter_invalid", "当前学科章节来源无效"
            )
        source_count = len({str(value) for value in sources if str(value).strip()})
        if source_count > 0:
            weights[name] = source_count
    if not weights:
        raise PastV3ProductionBindingError(
            "catalog_chapter_authority_empty", "当前学科没有可用章节来源权重"
        )
    chapter_identity = _digest(
        {
            "catalog_version": catalog_version,
            "course_version": course_version,
            "subject": subject_name,
            "weights": weights,
        }
    )
    entries = {
        "chapter_weight": _authority_entry(
            f"{CHAPTER_POLICY_VERSION}:{chapter_identity}",
            {"subject": subject_name, "weights": weights},
        ),
        "diversity": _authority_entry(
            DIVERSITY_POLICY_VERSION, {"max_per_source_exam": 1}
        ),
        "coverage": _authority_entry(COVERAGE_POLICY_VERSION, {"minima": []}),
        "uniqueness": _authority_entry(
            UNIQUENESS_POLICY_VERSION,
            {"resource_keys": ["candidate_id", "content_hash"]},
        ),
    }
    body = {
        "registry_version": AUTHORITY_REGISTRY_VERSION,
        "source_identity": dict(source_identity),
        "entries": entries,
    }
    return {**body, "registry_hash": _digest(body)}


def build_recent_usage_snapshot(
    db: Any, *, subject_id: int, limit: int = RECENT_USAGE_LIMIT
) -> dict[str, Any]:
    """Freeze the latest subject-local immutable native usage rows."""

    if type(subject_id) is not int or subject_id <= 0:
        raise PastV3ProductionBindingError(
            "recent_usage_subject_invalid", "最近使用快照学科无效"
        )
    if type(limit) is not int or not 1 <= limit <= RECENT_USAGE_LIMIT:
        raise PastV3ProductionBindingError(
            "recent_usage_limit_invalid", "最近使用快照窗口无效"
        )
    rows = db.execute(
        f"SELECT u.job_id,u.exam_id,u.position,u.candidate_id,u.content_hash,"
        f"u.usage_hash,u.payload_json,u.created_at FROM {USAGE} u "
        "JOIN exams e ON e.id=u.exam_id WHERE e.subject_id=? "
        "ORDER BY u.created_at DESC,u.job_id DESC,u.position DESC LIMIT ?",
        (subject_id, limit),
    ).fetchall()
    usage_entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PastV3ProductionBindingError(
                "recent_usage_payload_invalid", "最近使用账本载荷无效"
            ) from exc
        row_body = {
            "version": USAGE_VERSION,
            "job_id": str(row["job_id"]),
            "exam_id": int(row["exam_id"]),
            "position": int(row["position"]),
            "candidate_id": str(row["candidate_id"]),
            "content_hash": str(row["content_hash"]),
            "publication_hash": payload.get("publication_hash"),
        }
        if (
            payload != row_body
            or not hmac.compare_digest(str(row["usage_hash"]), _digest(row_body))
        ):
            raise PastV3ProductionBindingError(
                "recent_usage_hash_mismatch", "最近使用账本身份或哈希漂移"
            )
        usage_entries.append({
            "candidate_id": row_body["candidate_id"], "content_hash": row_body["content_hash"],
            "usage_hash": str(row["usage_hash"]), "created_at": str(row["created_at"]),
            "job_id": row_body["job_id"], "position": row_body["position"],
        })
    from .exam_generation_d2_publication import published_d2_usage

    usage_entries.extend(published_d2_usage(db, subject_id=subject_id, limit=limit))
    usage_entries.sort(key=lambda item: (item["created_at"], item["job_id"], item["position"]), reverse=True)
    usage_entries = usage_entries[:limit]
    exact_uses = [
        {
            "usage_hash": item["usage_hash"],
            "content_hash": item["content_hash"],
            "used_at": item["created_at"],
            "job_id": item["job_id"],
            "position": item["position"],
            "order_rank": rank,
        }
        for rank, item in enumerate(usage_entries, start=1)
    ]
    if len({item["usage_hash"] for item in exact_uses}) != len(exact_uses):
        raise PastV3ProductionBindingError(
            "recent_usage_identity_duplicate", "最近使用账本身份重复"
        )
    ledger_hash = _digest(
        {"mode": "past", "subject_id": subject_id, "limit": limit, "rows": exact_uses}
    )
    body = {
        "snapshot_version": EXACT_HISTORY_SNAPSHOT_VERSION,
        "mode": "past",
        "subject_id": subject_id,
        "window_limit": limit,
        "snapshot_identity": (
            f"past:subject-{subject_id}:latest-{limit}:usage-sha256-{ledger_hash}"
        ),
        "exact_uses": exact_uses,
    }
    return {**body, "snapshot_hash": _digest(body)}


def _model_callback(
    job_ai_call: Callable[..., Any],
    *,
    task_policy: str,
    system_prompt: str,
    return_mapping: bool,
    schema_correction_prompts: Mapping[str, str] | None = None,
    response_format_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    | None = None,
) -> Callable[[Any], Any]:
    def invoke(request_value: Any, user_prefix: str) -> Any:
        if isinstance(request_value, str):
            request_json = request_value
            try:
                request_document = json.loads(request_value)
            except json.JSONDecodeError as exc:
                raise PastV3ProductionBindingError(
                    "model_request_invalid", "模型请求不是 JSON 对象"
                ) from exc
        else:
            request_json = _canonical(request_value)
            request_document = request_value
        if not isinstance(request_document, Mapping):
            raise PastV3ProductionBindingError(
                "model_request_invalid", "模型请求不是 JSON 对象"
            )
        response_format = (
            dict(response_format_factory(request_document))
            if response_format_factory is not None
            else {"type": "json_object"}
        )
        raw = job_ai_call(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_prefix + "\n" + request_json,
                },
            ],
            task="exam_generation",
            task_policy=task_policy,
            response_format=response_format,
            max_tokens=32_000,
        )
        parsed = _strict_json_object(raw)
        return parsed if return_mapping else _canonical(parsed)

    def callback(request_value: Any, _request_hash: str | None = None) -> Any:
        return invoke(
            request_value,
            "以下是冻结协议请求。只返回满足协议的 JSON 对象：",
        )

    if schema_correction_prompts is not None:
        correction_prompts = dict(schema_correction_prompts)

        def correct_schema(request_value: Any, reason_code: str) -> Any:
            instruction = correction_prompts.get(reason_code)
            if instruction is None:
                raise PastV3ProductionBindingError(
                    "schema_correction_reason_invalid",
                    "结构纠正原因不属于有限白名单",
                )
            return invoke(
                request_value,
                "上一响应未通过严格结构校验；不得沿用或局部补写上一响应。"
                "请根据下列有限错误类别，从冻结请求重新构造完整 JSON。"
                f"错误类别：{reason_code}。{instruction}",
            )

        setattr(callback, "correct_schema", correct_schema)

    return callback


def build_candidate_base_rubric_callback(
    job_ai_call: Callable[..., Any],
) -> Callable[[Any], Any]:
    """Create the opt-in model adapter for candidate-base rubric calls.

    This adapter is intentionally not wired into the legacy production
    authority loader until an approved utility policy and persistence path are
    available.  It exists so calibration and the future worker share exactly
    one prompt/schema binding.
    """

    if not callable(job_ai_call):
        raise PastV3ProductionBindingError(
            "rubric_model_callback_missing", "候选级 rubric 模型回调缺失"
        )
    return _model_callback(
        job_ai_call,
        task_policy="candidate_selection_rubric",
        system_prompt=_RUBRIC_PROMPT,
        return_mapping=True,
        schema_correction_prompts=_RUBRIC_SCHEMA_CORRECTION_PROMPTS,
        response_format_factory=_candidate_base_rubric_response_format,
    )


def _score_response_format(request: Mapping[str, Any]) -> dict[str, Any]:
    """Bind provider-side structure to one frozen score window.

    The authoritative Stage-6 parser remains the final identity and relational
    validator.  This schema only prevents avoidable JSON/count/type/range drift
    before the bounded correction budget is consumed.
    """

    edges = request.get("edges")
    request_hash = request.get("request_hash")
    if (
        not isinstance(edges, list)
        or not 1 <= len(edges) <= 16
        or not isinstance(request_hash, str)
        or len(request_hash) != 64
    ):
        raise PastV3ProductionBindingError(
            "score_schema_request_invalid",
            "评分结构约束无法绑定冻结窗口",
        )
    slot_ids: list[str] = []
    candidate_ids: list[str] = []
    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise PastV3ProductionBindingError(
                "score_schema_request_invalid",
                "评分结构约束无法绑定冻结边",
            )
        slot_id = edge.get("slot_id")
        candidate_id = edge.get("candidate_id")
        pair = (slot_id, candidate_id)
        if (
            not isinstance(slot_id, str)
            or not slot_id
            or not isinstance(candidate_id, str)
            or not candidate_id
            or pair in pairs
        ):
            raise PastV3ProductionBindingError(
                "score_schema_request_invalid",
                "评分结构约束包含无效或重复边",
            )
        pairs.add(pair)
        slot_ids.append(slot_id)
        candidate_ids.append(candidate_id)
    reason_codes = [
        "strong_match",
        "acceptable_match",
        "weak_match",
        "content_risk",
        "source_risk",
        "format_risk",
    ]
    score_item = {
        "type": "object",
        "properties": {
            "slot_id": {"type": "string", "enum": sorted(set(slot_ids))},
            "candidate_id": {
                "type": "string",
                "enum": sorted(set(candidate_ids)),
            },
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "penalty": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason_codes": {
                "type": "array",
                "items": {"type": "string", "enum": reason_codes},
                "minItems": 1,
                "maxItems": len(reason_codes),
            },
        },
        "required": [
            "slot_id",
            "candidate_id",
            "score",
            "penalty",
            "reason_codes",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "name": "past_v3_stage6_score_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "version": {
                    "type": "string",
                    "enum": [SCORE_RESPONSE_VERSION],
                },
                "request_hash": {"type": "string", "enum": [request_hash]},
                "scores": {
                    "type": "array",
                    "items": score_item,
                    "minItems": len(edges),
                    "maxItems": len(edges),
                },
            },
            "required": ["version", "request_hash", "scores"],
            "additionalProperties": False,
        },
    }


def _difficulty_prompt(round_name: str) -> str:
    return (
        "你是严格真题候选难度判定器。只依据请求中的冻结题面与结构事实，"
        "逐个 candidate_id 返回结果；不得返回答案、解析、选择结论或额外字段。"
        f"当前轮次必须是 {round_name}。difficulty 只能是简单、中等、困难；"
        "confidence 必须是 0 到 1 的数值。若题面确实缺失或无法可靠判断，"
        "不得猜测，返回 difficulty=null、confidence=0。schema_version 的值必须逐字"
        "复制请求中的 response_schema_version，round 的值必须逐字复制请求中的 round，"
        "并精确覆盖所有候选。输出必须是单个"
        " JSON 对象，顶层键只能是 schema_version、round、results；results 中每个"
        "对象只能是 candidate_id、difficulty、confidence，数量必须与本次请求候选"
        "数量相同。格式示例（候选 ID、版本和轮次按请求原样替换）："
        '{"schema_version":"<response_schema_version>","round":"<round>",'
        '"results":[{"candidate_id":"<candidate_id>","difficulty":"中等",'
        '"confidence":0.75}]}。禁止使用 response_schema_version、classifications、'
        "request_hash、type 等字段或别名；禁止 Markdown 代码围栏、说明文字和额外字段。"
    )


def _difficulty_callback(
    job_ai_call: Callable[..., Any], *, round_name: str
) -> Callable[[Any], Any]:
    base_callback = _model_callback(
        job_ai_call,
        task_policy="difficulty_review",
        system_prompt=_difficulty_prompt(round_name),
        return_mapping=True,
    )

    def callback(request_value: Any, request_hash: str | None = None) -> Any:
        if isinstance(request_value, str):
            try:
                request_document = json.loads(request_value)
            except json.JSONDecodeError as exc:
                raise PastV3ProductionBindingError(
                    "model_request_invalid", "模型请求不是 JSON 对象"
                ) from exc
        else:
            request_document = request_value
        candidates = (
            request_document.get("candidates")
            if isinstance(request_document, Mapping)
            else None
        )
        if not isinstance(candidates, list) or len(candidates) <= 16:
            return base_callback(request_value, request_hash)

        merged_results: list[Any] = []
        merged_version: Any = None
        merged_round: Any = None
        for offset in range(0, len(candidates), DIFFICULTY_CALLBACK_CHUNK_SIZE):
            chunk_request = dict(request_document)
            chunk_request["candidates"] = candidates[
                offset : offset + DIFFICULTY_CALLBACK_CHUNK_SIZE
            ]
            chunk = base_callback(chunk_request, request_hash)
            if not isinstance(chunk, Mapping) or set(chunk) != {
                "schema_version", "round", "results"
            }:
                raise PastV3ProductionBindingError(
                    "difficulty_response_invalid",
                    "分块难度响应包含缺失或未授权字段",
                )
            results = chunk.get("results")
            if not isinstance(results, list):
                raise PastV3ProductionBindingError(
                    "difficulty_response_invalid", "分块难度结果不是数组"
                )
            if merged_version is None:
                merged_version = chunk.get("schema_version")
                merged_round = chunk.get("round")
            elif (
                chunk.get("schema_version") != merged_version
                or chunk.get("round") != merged_round
            ):
                raise PastV3ProductionBindingError(
                    "difficulty_response_invalid", "分块难度响应轮次或版本不一致"
                )
            merged_results.extend(results)
        return {
            "schema_version": merged_version,
            "round": merged_round,
            "results": merged_results,
        }

    return callback


_SCORE_PROMPT = (
    "你是严格真题候选匹配评分器。完整覆盖请求 edges，按每条边对目标槽位的"
    "题型、章节、来源与题面适配程度给出 0..100 整数 score 和不超过 score 的"
    "非负整数 penalty。reason_codes 只能使用 strong_match、acceptable_match、"
    "weak_match、content_risk、source_risk、format_risk，至少一个且不得重复。"
    "不得输出选择、难度、答案、解析、改编正文、推理或任何额外字段。响应 JSON"
    " 顶层键集合必须恰好为 version、request_hash、scores；必须把每条请求 edges"
    " 的评分放入 scores 数组，禁止把返回数组命名为 edges。scores 中每个对象的"
    "键集合必须恰好为 slot_id、candidate_id、score、penalty、reason_codes。响应"
    "中的 slot_id 与 candidate_id 必须从对应请求边逐字复制，不得改写、新增、"
    "遗漏或重复；scores 条数必须与 edges 条数完全相同，并保持与请求 edges"
    "相同的顺序。先读取 edges 的确切条数，再从第一条到最后一条各生成且只生成"
    "一个评分对象；输出前重新计数并逐项核对二元边身份、整数范围和原因码白名单。"
    f" version 必须为 {SCORE_RESPONSE_VERSION}，request_hash 必须原样回传。"
)


def _candidate_base_rubric_response_format(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the provider JSON schema for the new candidate-base contract.

    The schema is deliberately separate from ``_score_response_format``.  It
    contains no absolute score, penalty, utility or selection field; those
    values can only be produced by local code after a complete approved policy
    is supplied.
    """

    if not isinstance(request, Mapping):
        raise PastV3ProductionBindingError(
            "rubric_schema_request_invalid", "候选级 rubric 请求不是对象"
        )
    candidates = request.get("candidates")
    request_hash = request.get("request_hash")
    if (
        not isinstance(candidates, list)
        or not candidates
        or not isinstance(request_hash, str)
        or len(request_hash) != 64
        or request.get("version")
        != "past-v3-stage6-candidate-base-rubric-window-request-v1"
        or request.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or request.get("catalog_hash") != FACTOR_CATALOG_HASH
        or request.get("factor_applicability_version")
        != FACTOR_APPLICABILITY_VERSION
    ):
        raise PastV3ProductionBindingError(
            "rubric_schema_request_invalid", "候选级 rubric schema 无法绑定冻结请求"
        )
    def valid_hash(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    for field in ("graph_hash", "window_hash", "material_hash"):
        if not valid_hash(request.get(field)):
            raise PastV3ProductionBindingError(
                "rubric_schema_request_invalid", "候选级 rubric 请求哈希无效"
            )

    candidate_ids: list[str] = []
    candidate_items: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise PastV3ProductionBindingError(
                "rubric_schema_candidate_invalid", "候选级 rubric 请求条目无效"
            )
        identity_fields = (
            "request_hash", "candidate_id", "candidate_fact_hash", "content_hash",
            "content_snapshot_hash", "material_identity",
            "catalog_version", "catalog_hash",
            "factor_applicability_version",
        )
        if any(field not in candidate for field in identity_fields):
            raise PastV3ProductionBindingError(
                "rubric_schema_candidate_invalid", "候选级 rubric 身份字段缺失"
            )
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise PastV3ProductionBindingError(
                "rubric_schema_candidate_invalid", "候选级 rubric 候选身份重复或无效"
            )
        try:
            candidate_base_key(candidate)
        except CandidateBaseRubricError as exc:
            raise PastV3ProductionBindingError(
                "rubric_schema_candidate_invalid", "候选级 rubric 身份字段无效"
            ) from exc
        if not valid_hash(candidate.get("request_hash")):
            raise PastV3ProductionBindingError(
                "rubric_schema_candidate_invalid", "候选级 rubric request hash 无效"
            )
        candidate_ids.append(candidate_id)

    factors_schema: dict[str, Any] = {}
    for factor_id, specification in FACTOR_CATALOG["factors"].items():
        factors_schema[factor_id] = {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": list(specification["levels"])},
                "evidence_codes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": sorted(
                            set(specification["positive_evidence_codes"])
                            | set(specification["negative_evidence_codes"])
                        ),
                    },
                    "minItems": 1,
                    "maxItems": len(specification["positive_evidence_codes"])
                    + len(specification["negative_evidence_codes"]),
                },
                "reason_codes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(specification["reason_codes"]),
                    },
                    "minItems": 1,
                    "maxItems": len(specification["reason_codes"]),
                },
            },
            "required": ["level", "evidence_codes", "reason_codes"],
            "additionalProperties": False,
        }

    candidate_request_hashes = [
        str(candidate["request_hash"])
        for candidate in candidates
        if isinstance(candidate.get("request_hash"), str)
    ]
    if (
        len(candidate_request_hashes) != len(candidates)
        or len(set(candidate_request_hashes)) != len(candidate_request_hashes)
        or any(not valid_hash(value) for value in candidate_request_hashes)
    ):
        raise PastV3ProductionBindingError(
            "rubric_schema_candidate_invalid", "候选级 rubric request hash 缺失"
        )

    def candidate_values(field: str) -> list[Any]:
        values: list[Any] = []
        for candidate in candidates:
            value = candidate.get(field)
            if value not in values:
                values.append(value)
        return values

    rubric_properties = {
        "version": {"type": "string", "enum": [RUBRIC_RESPONSE_VERSION]},
        # Each rubric binds its candidate-local request.  The outer response
        # still binds the window request below; parse_window_rubric_response
        # enforces the exact candidate/hash pairing after schema validation.
        "request_hash": {
            "type": "string",
            "enum": candidate_request_hashes,
        },
        "candidate_id": {"type": "string", "enum": candidate_ids},
        "candidate_fact_hash": {
            "type": "string", "enum": candidate_values("candidate_fact_hash")
        },
        "content_hash": {
            "type": "string", "enum": candidate_values("content_hash")
        },
        "content_snapshot_hash": {
            "type": ["string", "null"],
            "enum": candidate_values("content_snapshot_hash"),
        },
        "material_identity": {
            "type": ["string", "null"],
            "enum": candidate_values("material_identity"),
        },
        "catalog_version": {"type": "string", "enum": [RUBRIC_CATALOG_VERSION]},
        "catalog_hash": {"type": "string", "enum": [FACTOR_CATALOG_HASH]},
        "factor_applicability_version": {
            "type": "string", "enum": [FACTOR_APPLICABILITY_VERSION]
        },
        "factors": {
            "type": "object",
            "properties": factors_schema,
            "required": sorted(factors_schema),
            "additionalProperties": False,
        },
    }
    rubric_item = {
        "type": "object",
        "properties": rubric_properties,
        "required": list(rubric_properties),
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "name": "past_v3_stage6_candidate_base_rubric_response",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "version": {
                    "type": "string", "enum": [WINDOW_RUBRIC_RESPONSE_VERSION]
                },
                "request_hash": {"type": "string", "enum": [request_hash]},
                "graph_hash": {
                    "type": "string", "enum": [request["graph_hash"]]
                },
                "window_hash": {
                    "type": "string", "enum": [request["window_hash"]]
                },
                "material_hash": {
                    "type": "string", "enum": [request["material_hash"]]
                },
                "catalog_version": {
                    "type": "string", "enum": [RUBRIC_CATALOG_VERSION]
                },
                "catalog_hash": {"type": "string", "enum": [FACTOR_CATALOG_HASH]},
                "factor_applicability_version": {
                    "type": "string", "enum": [FACTOR_APPLICABILITY_VERSION]
                },
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"rubric": rubric_item},
                        "required": ["rubric"],
                        "additionalProperties": False,
                    },
                    "minItems": len(candidates),
                    "maxItems": len(candidates),
                },
            },
            "required": [
                "version", "request_hash", "graph_hash", "window_hash",
                "material_hash", "catalog_version", "catalog_hash",
                "factor_applicability_version", "candidates",
            ],
            "additionalProperties": False,
        },
    }


_RUBRIC_PROMPT = (
    "你是候选题面质量 rubric 审查器。只依据请求中每个候选的冻结结构化题面，"
    "逐候选返回有限 rubric 等级、reason_codes 和 evidence_codes。当前只允许评价"
    "content_completeness 与 statement_clarity；question_type_family、target_difficulty、"
    "来源配额、章节配额、历史使用、相似度和槽位位置均不是 rubric 因素。不得输出"
    "绝对分数、扣减值、utility、选择结论、难度、答案、解析或推理。每个候选只能"
    "返回协议规定的身份字段和两个因素；等级与证据码必须来自请求目录，不能猜测"
    "缺失事实。必须原样绑定 request_hash、catalog_hash 和 factor_applicability_version。"
)

_RUBRIC_SCHEMA_CORRECTION_PROMPTS = {
    "schema_invalid_json": "只输出一个严格 JSON 对象，不要代码围栏或说明文字。",
    "schema_identity_mismatch": "按请求候选逐项原样复制身份，每个候选恰好一次，不得增删或改写。",
    "schema_forbidden_field": "删除所有绝对分数、扣减值、utility、选择、答案、难度和推理字段，只保留协议白名单。",
    "schema_validation_failed": "重新按目录输出有限等级、reason_codes 和 evidence_codes，不能使用未知因素或未知代码。",
}


def candidate_base_rubric_response_format(request: Mapping[str, Any]) -> dict[str, Any]:
    """Public, provider-neutral wrapper for the candidate-base JSON schema."""

    return _candidate_base_rubric_response_format(request)


def candidate_base_rubric_prompt() -> str:
    """Return the immutable prompt text used by the candidate-base adapter."""

    return _RUBRIC_PROMPT

_SCORE_SCHEMA_CORRECTION_PROMPTS = {
    "schema_invalid_json": (
        "上一响应不是严格 JSON。重新输出单个 JSON 对象，不得带代码围栏、说明文字"
        "或 JSON 之外的任何字符。"
    ),
    "schema_identity_mismatch": (
        "上一响应的 scores 数量或二元边身份集合不符。完全丢弃上一数组，严格按"
        "请求 edges 的原顺序逐条复制 slot_id 与 candidate_id；每条边恰好一次，"
        "不得新增、遗漏、重复或改写。"
    ),
    "schema_forbidden_field": (
        "上一响应包含越权字段。只保留协议明示的顶层三个键以及评分条目的五个键；"
        "不得返回选择、难度、答案、解析、改编正文或推理。"
    ),
    "schema_validation_failed": (
        "上一响应的字段、数值或原因码未通过校验。重新构造完整 scores；score 必须"
        "为 0..100 整数，penalty 必须为 0..score 整数，reason_codes 必须非空、"
        "不重复且只使用系统提示中的白名单。"
    ),
}

_QUALITY_PROMPT = (
    "你是正式原题的客观渲染审查器。请求中的题目来自已确认正式题库；其原始题意、"
    "表达正确性、教学适切性和可解性均属于受信任事实，不评价、不改写，也不得以"
    "语言风格、教学价值、难度观感或可解性作为不通过理由。唯一审查项是题面是否"
    "存在可客观复现、会阻止 TeX/PDF 正常形成的渲染故障；没有这种故障必须通过。"
    "精确覆盖所有 position；通过时 reason_codes 只能为 [\"none\"]，不通过时"
    "只能为 [\"rendering_risk\"]。不修改题面，不返回答案、解析或推理。响应 JSON"
    "顶层键集合必须恰好为 version、selection_hash、"
    "pre_adaptation_hash、results；必须把逐题结论放入 results 数组，禁止把返回"
    "数组命名为 items。results 中每个对象的键集合必须恰好为 position、passed、"
    "reason_codes。原样绑定 version、selection_hash 和 pre_adaptation_hash。"
)

_ADAPTATION_PROMPT = (
    "你是受控单题题面编辑器。只改善清晰性与成卷可用性，保持数学含义、条件、"
    "选项和求解目标不变；不得返回答案、解析、难度或推理。只返回协议规定的六个"
    "字段，原样绑定 version、position、candidate_id、pre_adaptation_hash、"
    "binding_hash，并把完整新题面放入 adapted_content。"
)

_VERIFICATION_PROMPT = (
    "你是改编题面的隐藏核验器。可以在内部检查可解性、语义完整、结果有效及可"
    "发布性，但不得返回答案、解法或推理。只返回协议规定字段；通过时"
    "reason_codes=[\"none\"]，不通过时仅使用 not_solvable、meaning_incomplete、"
    "result_invalid、not_publishable，并原样绑定所有身份哈希。"
)


def _artifact_callback(app: Any, context: Any) -> Callable[[Mapping[str, Any]], Any]:
    artifact_root = Path(str(app.config["PDF_DIR"])).resolve()
    command = str(app.config.get("XELATEX_COMMAND") or "xelatex")
    timeout = int(app.config.get("XELATEX_TIMEOUT", 60))

    def callback(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise PastV3ProductionBindingError(
                "artifact_request_invalid", "原生制品请求无效"
            )
        exam_id = payload.get("exam_id")
        request_hash = payload.get("request_hash")
        latex_source = payload.get("latex_source")
        if (
            type(exam_id) is not int
            or exam_id <= 0
            or not isinstance(request_hash, str)
            or len(request_hash) != 64
            or not isinstance(latex_source, str)
        ):
            raise PastV3ProductionBindingError(
                "artifact_request_invalid", "原生制品请求身份无效"
            )
        tex_path = Path(str(payload.get("tex_path") or "")).resolve()
        pdf_path = Path(str(payload.get("pdf_path") or "")).resolve()
        if tex_path.parent != artifact_root or pdf_path.parent != artifact_root:
            raise PastV3ProductionBindingError(
                "artifact_path_outside_root", "原生制品路径越出 PDF 目录"
            )
        stem = f"v3_exam_{exam_id}_{request_hash[:16]}"
        compiled_pdf = artifact_root / f"{stem}.pdf"
        try:
            compile_latex_artifact(
                str(artifact_root),
                command,
                stem,
                latex_source,
                timeout=timeout,
                runner=context.run_cancellable_process,
                publish_source=False,
            )
            compiled_pdf.replace(pdf_path)
        finally:
            try:
                compiled_pdf.unlink(missing_ok=True)
            except OSError:
                pass
            for temporary in artifact_root.glob(f".{stem}-*"):
                if temporary.is_dir():
                    shutil.rmtree(temporary, ignore_errors=True)
        return {
            "version": ARTIFACT_VERSION,
            "request_hash": request_hash,
            "exam_id": exam_id,
        }

    return callback


def acknowledge_publication_receipt(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"publication_hash": body.get("publication_hash")}


def load_past_v3_production_authorities(
    *, app: Any, context: Any, request: Any, preflight: Any,
    job_ai_call: Callable[..., Any]
) -> PastV3ProductionAuthorities:
    """Resolve one attempt without changing application or AI configuration."""

    profile_context = getattr(preflight, "past_profile_context", None)
    if profile_context is None or getattr(request, "source", None) != "past":
        raise PastV3ProductionBindingError(
            "past_profile_context_missing", "严格真题生产画像上下文缺失"
        )
    pending = getattr(request, "pending_memory_request", None)
    payload = pending.get("request") if isinstance(pending, Mapping) else None
    if not isinstance(payload, Mapping):
        raise PastV3ProductionBindingError(
            "frozen_request_missing", "严格真题冻结请求缺失"
        )
    subject_id = payload.get("subject_id")
    if type(subject_id) is not int or subject_id <= 0:
        raise PastV3ProductionBindingError(
            "subject_id_invalid", "严格真题学科 ID 无效"
        )
    profile = profile_context.to_dict()
    binding = profile.get("profile_binding")
    combination = binding.get("combination") if isinstance(binding, Mapping) else None
    if not isinstance(combination, Mapping):
        raise PastV3ProductionBindingError(
            "profile_combination_missing", "冻结画像组合身份缺失"
        )
    db = get_db()
    subject_row = db.execute(
        "SELECT id,name FROM subjects WHERE id=?", (subject_id,)
    ).fetchone()
    subject_name = str(subject_row["name"] or "").strip() if subject_row else ""
    if subject_name != str(combination.get("subject") or "").strip():
        raise PastV3ProductionBindingError(
            "subject_profile_mismatch", "冻结画像学科与请求学科不一致"
        )
    catalog = app.extensions.get("catalog_service")
    if catalog is None:
        raise PastV3ProductionBindingError(
            "catalog_service_missing", "目录服务尚未配置"
        )
    public_settings = get_settings_service(app).public_ai_settings()
    model_id = str(public_settings.get("quality_model") or "").strip()
    if not model_id:
        raise PastV3ProductionBindingError(
            "quality_model_missing", "质量模型尚未配置"
        )

    classify = _difficulty_callback(job_ai_call, round_name="classification")
    difficulty_verify = _difficulty_callback(job_ai_call, round_name="verification")
    requested_protocol = getattr(request, "stage6_scoring_protocol", None)
    if requested_protocol is None:
        requested_protocol = "legacy_score_read_only_v2"
    if requested_protocol not in {"legacy_score_read_only_v2", "candidate_base_rubric_v1", D2_SCORING_PROTOCOL}:
        raise PastV3ProductionBindingError(
            "scoring_protocol_unknown", "请求携带了不受支持的阶段 6 评分协议"
        )
    if requested_protocol == "candidate_base_rubric_v1":
        # A new request never receives the legacy score/penalty callback.  The
        # worker will still fail closed until an E3 utility policy and durable
        # rubric receipt path are supplied.
        score = None
        rubric = build_candidate_base_rubric_callback(job_ai_call)
    else:
        # User-frozen 2026-08-30 rule: legacy score/penalty is historical
        # read-only recovery data.  A new or incomplete strict task never
        # receives a callback capable of producing another legacy receipt.
        score = None
        rubric = None
    quality_model = _model_callback(
        job_ai_call,
        task_policy="exam_quality_review",
        system_prompt=_QUALITY_PROMPT,
        return_mapping=False,
    )

    def quality(request_value: Any, request_hash: str | None = None) -> Any:
        try:
            request = (
                json.loads(request_value)
                if isinstance(request_value, str)
                else dict(request_value)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PastV3ProductionBindingError(
                "quality_request_invalid", "正式原题质量请求无效"
            ) from exc
        expected_policy = formal_original_quality_policy()
        if (
            request.get("version") != QUALITY_PROTOCOL_VERSION
            or request.get("allowed_reason_codes") != ["none", "rendering_risk"]
            or request.get("review_policy") != expected_policy
        ):
            raise PastV3ProductionBindingError(
                "quality_policy_superseded",
                "质量请求不符合正式原题客观渲染审查边界",
            )
        return quality_model(request, request_hash)
    allow_adaptation = bool(getattr(request, "allow_adaptation", False))
    adaptation = (
        _model_callback(
            job_ai_call,
            task_policy="exam_item_repair",
            system_prompt=_ADAPTATION_PROMPT,
            return_mapping=False,
        )
        if allow_adaptation
        else None
    )
    hidden_verification = (
        _model_callback(
            job_ai_call,
            task_policy="exam_quality_review",
            system_prompt=_VERIFICATION_PROMPT,
            return_mapping=False,
        )
        if allow_adaptation
        else None
    )

    def registry_loader(*, stage3: Mapping[str, Any], **_: Any) -> Mapping[str, Any]:
        return build_catalog_authority_registry(catalog, stage3=stage3)

    def recent_loader(**_: Any) -> Mapping[str, Any]:
        return build_recent_usage_snapshot(get_db(), subject_id=subject_id)

    def usage_loader(**_: Any) -> Mapping[str, Any]:
        snapshot = getattr(context, "snapshot_exam_metrics", None)
        metrics = snapshot() if callable(snapshot) else {}
        return {
            "call_counts": {
                "model_calls": int(metrics.get("model_calls", 0)),
                "provider_calls": int(metrics.get("provider_calls", 0)),
            },
            "retry_counts": {
                "model_retry_count": int(metrics.get("model_retry_count", 0))
            },
        }

    institution = str(combination.get("institution") or "").strip()
    reference_year = combination.get("reference_year")
    title = f"{institution}{reference_year}年{subject_name}真题风格卷"
    return PastV3ProductionAuthorities(
        profile_context=profile_context,
        authority_registry_loader=registry_loader,
        recent_usage_snapshot_loader=recent_loader,
        score_callback=score,
        score_prompt_version=SCORE_PROMPT_VERSION if score is not None else None,
        score_model_id=model_id if score is not None else None,
        quality_callback=quality,
        subject_id=subject_id,
        title=title,
        artifact_dir=Path(str(app.config["PDF_DIR"])),
        artifact_callback=_artifact_callback(app, context),
        classification_binding={
            "model": model_id,
            "prompt_version": DIFFICULTY_CLASSIFICATION_PROMPT_VERSION,
            "rules_hash": DIFFICULTY_RULES_HASH,
        },
        verification_binding={
            "model": model_id,
            "prompt_version": DIFFICULTY_VERIFICATION_PROMPT_VERSION,
            "rules_hash": DIFFICULTY_RULES_HASH,
        },
        classify_callback=classify,
        difficulty_verify_callback=difficulty_verify,
        score_provider_capability=1 if score is not None else None,
        score_transport_retry_limit=2,
        adaptation_callback=adaptation,
        verification_callback=hidden_verification,
        receipt_callback=app.extensions.get(
            PAST_V3_PUBLICATION_RECEIPT_EXTENSION, acknowledge_publication_receipt
        ),
        usage_loader=usage_loader,
        stage7_budget=Stage7Budget(
            mode="observe", structural_calls_remaining=3
        ),
        scoring_protocol=requested_protocol,
        rubric_callback=rubric,
        rubric_prompt_version=RUBRIC_PROMPT_VERSION if rubric is not None else None,
        rubric_model_id=model_id if rubric is not None else None,
        utility_policy=getattr(request, "stage6_utility_policy", None),
        priority_once=bool(getattr(request, "priority_once", False)),
        memory=memory_from_app(app),
        pending_memory_request=dict(pending),
        memory_subject=subject_name,
        memory_logger=getattr(app, "logger", None),
        render_probe=partial(
            probe_rendering_risk,
            compile_callback=make_production_compile_callback(
                xelatex_command=app.config.get("XELATEX_COMMAND", "xelatex"),
                timeout_seconds=app.config.get("XELATEX_TIMEOUT", 60),
            ),
        ),
    )


def register_past_v3_production_dependencies(app: Any) -> bool:
    """Register the production factory only for a fully configured app."""

    extensions = getattr(app, "extensions", None)
    if not isinstance(extensions, dict) or extensions.get("catalog_service") is None:
        return False
    extensions.setdefault(
        PAST_V3_PUBLICATION_RECEIPT_EXTENSION, acknowledge_publication_receipt
    )
    if PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION not in extensions:
        extensions[PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION] = (
            make_past_v3_production_dependencies_factory(
                load_past_v3_production_authorities
            )
        )
    return True


__all__ = [
    "DIFFICULTY_RULES_HASH",
    "PastV3ProductionBindingError",
    "RUBRIC_PROMPT_VERSION",
    "build_candidate_base_rubric_callback",
    "candidate_base_rubric_prompt",
    "candidate_base_rubric_response_format",
    "build_catalog_authority_registry",
    "build_recent_usage_snapshot",
    "load_past_v3_production_authorities",
    "register_past_v3_production_dependencies",
]
