"""Explicit, provider-free local factor receipt contract (D2-v1).

The validator is intentionally an in-memory seam.  Callers must provide the
candidate/content identities and evidence; no value is inferred from rubric
fields, memory storage, or a provider response.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .exam_generation_stage6_objective import (
    FACTOR_LEVELS,
    FACTOR_ORDER,
    validate_objective_policy,
    CandidateBaseObjectiveError,
)

LOCAL_FACTOR_RECEIPT_VERSION = "candidate-local-factor-receipt-v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class LocalFactorReceiptError(ValueError):
    """Raised when an explicit local factor receipt is not verifiable."""


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or _HEX64.fullmatch(value) is None:
        raise LocalFactorReceiptError(f"local_factor_{field}_invalid")
    return value


def _evidence_hash(candidate_id: str, fact_hash: str, content_hash: str, policy_hash: str,
                   factor: str, level: str, codes: list[str]) -> str:
    return _hash({"candidate_id": candidate_id, "candidate_fact_hash": fact_hash,
                  "content_hash": content_hash, "policy_hash": policy_hash,
                  "factor": factor, "level": level, "evidence_codes": codes})


def validate_local_factor_receipt(receipt: Mapping[str, Any], *, policy: Mapping[str, Any],
                                  expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and canonicalize one explicit D2 local-factor receipt.

    ``expected`` may bind candidate/fact/content identities from a frozen graph.
    Missing optional memory/history evidence remains an explicit caller choice
    (usually ``not_applicable``), never an implicit zero.
    """
    try:
        p = validate_objective_policy(policy)
    except CandidateBaseObjectiveError as exc:
        raise LocalFactorReceiptError(str(exc)) from exc
    required = {"version", "policy_hash", "candidate_id", "candidate_fact_hash", "content_hash", "factors", "receipt_hash"}
    if not isinstance(receipt, Mapping) or set(receipt) != required:
        raise LocalFactorReceiptError("local_factor_receipt_shape_invalid")
    if receipt["version"] != LOCAL_FACTOR_RECEIPT_VERSION or receipt["policy_hash"] != p["policy_hash"]:
        raise LocalFactorReceiptError("local_factor_policy_binding_invalid")
    cid = receipt["candidate_id"]
    fh = _identity(receipt["candidate_fact_hash"], "candidate_fact_hash")
    ch = _identity(receipt["content_hash"], "content_hash")
    if not isinstance(cid, str) or not cid.strip():
        raise LocalFactorReceiptError("local_factor_candidate_id_invalid")
    if expected is not None:
        for field, actual in (("candidate_id", cid), ("candidate_fact_hash", fh), ("content_hash", ch)):
            if field in expected and expected[field] != actual:
                raise LocalFactorReceiptError("local_factor_identity_mismatch")
    factors = receipt["factors"]
    if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_ORDER):
        raise LocalFactorReceiptError("local_factor_set_invalid")
    clean: dict[str, Any] = {}
    for factor in FACTOR_ORDER:
        item = factors[factor]
        if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes", "evidence_hash"}:
            raise LocalFactorReceiptError("local_factor_shape_invalid")
        level, codes, ev_hash = item["level"], item["evidence_codes"], item["evidence_hash"]
        if level not in FACTOR_LEVELS[factor] or (level == "unknown" and p["unknown_blocked"]):
            raise LocalFactorReceiptError("local_factor_unknown_blocked" if level == "unknown" else "local_factor_level_invalid")
        if not isinstance(codes, list) or not codes or any(not isinstance(x, str) or not x for x in codes) or len(set(codes)) != len(codes):
            raise LocalFactorReceiptError("local_factor_evidence_invalid")
        canonical_codes = sorted(codes)
        if not isinstance(ev_hash, str) or not _HEX64.fullmatch(ev_hash) or ev_hash != _evidence_hash(cid, fh, ch, p["policy_hash"], factor, level, canonical_codes):
            raise LocalFactorReceiptError("local_factor_evidence_hash_invalid")
        clean[factor] = {"level": level, "evidence_codes": canonical_codes, "evidence_hash": ev_hash}
    body = {"version": LOCAL_FACTOR_RECEIPT_VERSION, "policy_hash": p["policy_hash"], "candidate_id": cid,
            "candidate_fact_hash": fh, "content_hash": ch, "factors": clean}
    if receipt["receipt_hash"] != _hash(body):
        raise LocalFactorReceiptError("local_factor_receipt_hash_invalid")
    return {**body, "receipt_hash": receipt["receipt_hash"]}


def build_local_factor_receipt(*, candidate_id: str, candidate_fact_hash: str, content_hash: str,
                               factors: Mapping[str, Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Build a receipt only from caller-supplied factor levels and evidence."""
    p = validate_objective_policy(policy)
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise LocalFactorReceiptError("local_factor_candidate_id_invalid")
    cid, fh, ch = candidate_id, _identity(candidate_fact_hash, "candidate_fact_hash"), _identity(content_hash, "content_hash")
    prepared = {}
    for factor in FACTOR_ORDER:
        item = factors.get(factor) if isinstance(factors, Mapping) else None
        if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes"}:
            raise LocalFactorReceiptError("local_factor_shape_invalid")
        level, codes = item["level"], item["evidence_codes"]
        if level not in FACTOR_LEVELS[factor] or (level == "unknown" and p["unknown_blocked"]):
            raise LocalFactorReceiptError("local_factor_unknown_blocked" if level == "unknown" else "local_factor_level_invalid")
        if not isinstance(codes, list) or not codes or any(not isinstance(x, str) or not x for x in codes) or len(set(codes)) != len(codes):
            raise LocalFactorReceiptError("local_factor_evidence_invalid")
        codes = sorted(codes)
        prepared[factor] = {"level": level, "evidence_codes": codes, "evidence_hash": _evidence_hash(cid, fh, ch, p["policy_hash"], factor, level, codes)}
    body = {"version": LOCAL_FACTOR_RECEIPT_VERSION, "policy_hash": p["policy_hash"], "candidate_id": cid,
            "candidate_fact_hash": fh, "content_hash": ch, "factors": prepared}
    return {**body, "receipt_hash": _hash(body)}


def validate_local_factor_receipts(receipts: Sequence[Mapping[str, Any]], *, policy: Mapping[str, Any],
                                   expected: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Validate a complete caller-supplied batch without inferring identities."""
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence) or not receipts:
        raise LocalFactorReceiptError("local_factor_receipts_invalid")
    if expected is not None and (not isinstance(expected, Sequence) or len(expected) != len(receipts)):
        raise LocalFactorReceiptError("local_factor_receipts_identity_invalid")
    result = [validate_local_factor_receipt(row, policy=policy, expected=(expected[i] if expected is not None else None))
              for i, row in enumerate(receipts)]
    keys = [(r["candidate_id"], r["candidate_fact_hash"], r["content_hash"]) for r in result]
    if len(set(keys)) != len(keys):
        raise LocalFactorReceiptError("local_factor_receipts_duplicate_identity")
    return result


def project_local_factor_receipts_for_d2(receipts: Sequence[Mapping[str, Any]], *, policy: Mapping[str, Any],
                                         expected: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Verify local receipts, then project only the legacy D2 solver shape.

    Evidence hashes are checked before projection and therefore cannot be
    dropped to bypass the local contract; the projection is intentionally
    ephemeral and carries candidate/content identity into the solver.
    """
    validated = validate_local_factor_receipts(receipts, policy=policy, expected=expected)
    return [{"candidate_id": row["candidate_id"], "candidate_fact_hash": row["candidate_fact_hash"],
             "content_hash": row["content_hash"],
             "factors": {factor: {"level": item["level"], "evidence_codes": list(item["evidence_codes"])}
                         for factor, item in row["factors"].items()}}
            for row in validated]


# Short alias for callers that handle one receipt at a time.
validate_factor_receipt = validate_local_factor_receipt


__all__ = ["LOCAL_FACTOR_RECEIPT_VERSION", "LocalFactorReceiptError", "build_local_factor_receipt", "validate_local_factor_receipt", "validate_factor_receipt", "validate_local_factor_receipts", "project_local_factor_receipts_for_d2"]
