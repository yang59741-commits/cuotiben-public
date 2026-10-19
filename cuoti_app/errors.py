"""Public v2 error envelope and request correlation identifiers."""

from __future__ import annotations

import re
import uuid

from flask import g, request
from .services.runtime_diagnostics import diagnostic_event


_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:\\[^\r\n\t\"']+")
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_SECRET_PAIR = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+"
)
PDF_OCR_DISABLED_CODE = "pdf_ocr_disabled"
PDF_OCR_DISABLED_MESSAGE = "PDF OCR 已永久停用；请改用 DOCX、TeX 或 TXT 本地提取"


def _safe_message(value: object, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = _WINDOWS_PATH.sub("[本机路径已隐藏]", text)
    text = _BEARER.sub("Bearer [已隐藏]", text)
    text = _SECRET_PAIR.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    return text[:800]


def init_error_contract(app) -> None:
    @app.before_request
    def assign_correlation_id():
        incoming = str(request.headers.get("X-Correlation-ID") or "").strip()
        g.correlation_id = (
            incoming
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", incoming)
            else uuid.uuid4().hex
        )
        diagnostic_event("request_start", method=request.method,
                         route=request.url_rule.rule if request.url_rule else "(unmatched)",
                         correlation_id=g.correlation_id)

    @app.after_request
    def normalize_v2_error(response):
        correlation_id = getattr(g, "correlation_id", uuid.uuid4().hex)
        diagnostic_event("request_end", status=response.status_code, correlation_id=correlation_id)
        response.headers["X-Correlation-ID"] = correlation_id
        if not request.path.startswith("/api/v2/") or response.status_code < 400:
            return response
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        raw_error = payload.get("error")
        raw_message = payload.get("message")
        if not raw_message and isinstance(raw_error, dict):
            raw_message = raw_error.get("message")
        if not raw_message and isinstance(raw_error, str):
            raw_message = raw_error
        message = _safe_message(
            raw_message, f"请求失败（HTTP {response.status_code}）"
        )
        nested_code = raw_error.get("code") if isinstance(raw_error, dict) else None
        code = str(payload.get("code") or nested_code or f"HTTP_{response.status_code}")[:80]
        retryable = bool(
            payload.get(
                "retryable",
                response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        )
        hints = {
            400: "请检查输入后重试",
            409: "请刷新最新数据并处理冲突",
            429: "请稍后重试",
            503: "请打开运行状态执行重新诊断",
        }
        normalized = dict(payload)
        normalized.update(
            {
                "error": message,
                "code": code,
                "message": message,
                "retryable": retryable,
                "hint": _safe_message(
                    payload.get("hint"), hints.get(response.status_code, "")
                ),
                "correlation_id": correlation_id,
            }
        )
        response.set_data(app.json.dumps(normalized))
        response.content_type = "application/json; charset=utf-8"
        return response
