"""Freeze optional task inputs once without mutating the submitted request."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Callable, Mapping, Sequence

from ..transactions import immediate_transaction

SOFT_INPUT_VERSION = "exam-soft-input-v1"
SOFT_INPUTS = "exam_generation_soft_inputs"


def freeze_soft_input(
    db, *, job_id: str, mode: str, input_key: str,
    allowed_sources: Sequence[str], loader: Callable[[], dict],
    guard: Callable | None = None,
) -> dict:
    """Return the first committed read, including an intentionally empty input.

    The loader is local/read-only and runs under the same lock as insertion.
    Recovering a damaged optional payload omits it; it never refreshes facts.
    """
    sources = sorted(set(allowed_sources))
    if not job_id or not input_key or mode not in {"free", "mistakes", "past"}:
        raise ValueError("invalid soft input task binding")
    allowed = {"free": {"free", "mistakes"}, "mistakes": {"mistakes"},
               "past": {"past", "mistakes"}}[mode]
    if not sources or not set(sources) <= allowed:
        raise ValueError("soft input source is outside the mode read scope")
    with immediate_transaction(db):
        if guard is not None:
            guard()
        row = db.execute(
            f"SELECT mode,allowed_sources_json,read_cutoff,payload_json,version "
            f"FROM {SOFT_INPUTS} WHERE job_id=? AND input_key=?", (job_id, input_key),
        ).fetchone()
        if row is not None:
            try:
                stored_sources = json.loads(row[1])
            except (TypeError, ValueError):
                stored_sources = None
            if row[0] != mode or stored_sources != sources:
                raise ValueError("soft input task mode or source scope changed")
            try:
                payload = json.loads(row[3])
                if not isinstance(payload, dict) or row[4] != SOFT_INPUT_VERSION:
                    raise ValueError("invalid stored optional payload")
            except (TypeError, ValueError):
                payload = {"state": "omitted", "reason": "frozen_soft_input_unusable"}
            return {"version": SOFT_INPUT_VERSION, "mode": mode,
                    "allowed_sources": sources, "read_cutoff": row[2], "payload": payload}
        payload = loader()
        cutoff = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if not isinstance(payload, Mapping):
            raise ValueError("soft input loader must return an object")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        db.execute(
            f"INSERT INTO {SOFT_INPUTS} "
            "(job_id,input_key,mode,allowed_sources_json,read_cutoff,payload_json,version) "
            "VALUES(?,?,?,?,?,?,?)",
            (job_id, input_key, mode, json.dumps(sources, separators=(",", ":")),
             cutoff, raw, SOFT_INPUT_VERSION),
        )
        return {"version": SOFT_INPUT_VERSION, "mode": mode,
                "allowed_sources": sources, "read_cutoff": cutoff,
                "payload": json.loads(raw)}
