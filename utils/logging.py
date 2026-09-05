from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_CONTEXT: dict[str, contextvars.ContextVar[str | None]] = {
    name: contextvars.ContextVar(name, default=None)
    for name in ("request_id", "account_id", "sync_run_id", "worker_id")
}

_RECORD_KEYS = {
    "args", "asctime", "created", "event", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info", "taskName",
    "thread", "threadName",
}

_SENSITIVE = ("authorization", "token", "secret", "password", "api_key", "apikey", "credential")


def redact(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in _SENSITIVE):
        return "[redacted]" if value not in (None, "") else value
    return value


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _safe(redact(str(key), item)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return str(value)


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {name: var.get() for name, var in _CONTEXT.items() if var.get()}
    for key, value in record.__dict__.items():
        if key in _RECORD_KEYS or key.startswith("_"):
            continue
        fields[key] = _safe(redact(key, value))
    return fields


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
        payload.update(_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<7} {record.name} "
            f"{getattr(record, 'event', None) or record.getMessage()}"
        )
        parts = [f"{key}={value}" for key, value in _fields(record).items()]
        if parts:
            line += "  " + " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else PlainFormatter())
    root.addHandler(handler)
    root.setLevel(_level(level))
    for noisy in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: str, event: str, **fields: Any) -> None:
    extra = {"event": event, **{key: _safe(redact(key, value)) for key, value in fields.items()}}
    logger.log(_level(level), event, extra=extra)


def bind_context(**values: str | None) -> list[tuple[contextvars.ContextVar, Any]]:
    return [
        (_CONTEXT[name], _CONTEXT[name].set(value))
        for name, value in values.items()
        if value is not None
    ]


def reset_context(tokens: list[tuple[contextvars.ContextVar, Any]]) -> None:
    for var, token in reversed(tokens):
        var.reset(token)


def _level(level: str) -> int:
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)
