from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
account_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("account_id", default=None)
sync_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("sync_run_id", default=None)
worker_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("worker_id", default=None)

_RESERVED_LOG_RECORD_KEYS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}

_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event

        for key, value in _context_fields().items():
            if value:
                payload[key] = value

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_KEYS or key in {"event"}:
                continue
            if key.startswith("_"):
                continue
            payload[key] = _json_safe(redact_if_sensitive(key, value))

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(_log_level(level))

    for noisy_logger in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    return logger


def log_event(
    logger: logging.Logger,
    level: str,
    event: str,
    message: str | None = None,
    **fields: Any,
) -> None:
    logger.log(_log_level(level), message or event, extra={"event": event, **_clean_fields(fields)})


def duration_ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def safe_preview(value: Any, max_chars: int = 200) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=True, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def hash_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def set_request_context(
    request_id: str | None = None,
    account_id: str | None = None,
) -> list[tuple[contextvars.ContextVar, Any]]:
    tokens: list[tuple[contextvars.ContextVar, Any]] = []
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(request_id)))
    if account_id is not None:
        tokens.append((account_id_var, account_id_var.set(account_id)))
    return tokens


def set_sync_context(
    *,
    sync_run_id: str | None = None,
    worker_id: str | None = None,
    account_id: str | None = None,
) -> list[tuple[contextvars.ContextVar, Any]]:
    tokens: list[tuple[contextvars.ContextVar, Any]] = []
    if sync_run_id is not None:
        tokens.append((sync_run_id_var, sync_run_id_var.set(sync_run_id)))
    if worker_id is not None:
        tokens.append((worker_id_var, worker_id_var.set(worker_id)))
    if account_id is not None:
        tokens.append((account_id_var, account_id_var.set(account_id)))
    return tokens


def reset_context(tokens: list[tuple[contextvars.ContextVar, Any]]) -> None:
    for var, token in reversed(tokens):
        var.reset(token)


def _context_fields() -> dict[str, str | None]:
    return {
        "request_id": request_id_var.get(),
        "account_id": account_id_var.get(),
        "sync_run_id": sync_run_id_var.get(),
        "worker_id": worker_id_var.get(),
    }


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(redact_if_sensitive(key, value)) for key, value in fields.items()}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(redact_if_sensitive(str(key), item)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def redact_if_sensitive(key: str, value: Any) -> Any:
    normalized = key.lower()
    if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
        return "[redacted]" if value not in (None, "") else value
    return value


def _log_level(level: str) -> int:
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)
