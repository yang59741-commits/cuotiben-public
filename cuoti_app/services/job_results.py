"""Atomic business-side result markers used to replay durable jobs safely."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone


JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def validated_job_id(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if not JOB_ID_PATTERN.fullmatch(text):
        raise ValueError("任务编号无效")
    return text


def load_business_result(db, job_id: str | None, kind: str):
    if not job_id:
        return None
    row = db.execute(
        "SELECT kind,result_json FROM job_business_results WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    if row["kind"] != kind:
        raise ValueError("任务结果类型不一致")
    return json.loads(row["result_json"])


def store_business_result(db, job_id: str | None, kind: str, result) -> None:
    if not job_id:
        return
    raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    committed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    db.execute(
        "INSERT INTO job_business_results(job_id,kind,result_json,committed_at) "
        "VALUES(?,?,?,?) ON CONFLICT(job_id) DO NOTHING",
        (job_id, kind, raw, committed_at),
    )
