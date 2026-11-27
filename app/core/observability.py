"""Safe structured application logging and request correlation."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}")
_REQUEST_ID = re.compile(r"[a-f0-9]{32}|-")
_FRAME = re.compile(r"[\w./<>-]+:\d+ in [\w.<>-]+")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})
_CODE_FIELDS = ("route", "error_type", "reason_code", "operation")
_INTEGER_FIELDS = (
    "status_code", "user_id", "entity_id", "provider_status", "count", "skipped_count",
)
_EXTRA_FIELDS = (
    *_CODE_FIELDS, *_INTEGER_FIELDS, "method", "elapsed_ms", "reason_counts", "safe_traceback",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_code(value: Any) -> bool:
    return isinstance(value, str) and _CODE.fullmatch(value) is not None


def _is_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


class JsonFormatter(logging.Formatter):
    """Serialize only explicitly-safe structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        outcome = getattr(record, "outcome", None)
        request_id = getattr(record, "request_id", request_id_var.get())
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            request_id = "-"
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "severity": record.levelname,
            "event": event if _is_code(event) else "application.log",
            "outcome": outcome if _is_code(outcome) else "info",
            "request_id": request_id,
        }
        for name in _CODE_FIELDS:
            value = getattr(record, name, None)
            if _is_code(value):
                payload[name] = value
        for name in _INTEGER_FIELDS:
            value = getattr(record, name, None)
            if _is_count(value):
                payload[name] = value

        method = getattr(record, "method", None)
        if isinstance(method, str):
            payload["method"] = method if method in _METHODS else "OTHER"
        elapsed_ms = getattr(record, "elapsed_ms", None)
        if type(elapsed_ms) in (float, int) and 0 <= elapsed_ms <= 2**63 - 1:
            payload["elapsed_ms"] = elapsed_ms
        counts = getattr(record, "reason_counts", None)
        if isinstance(counts, dict) and len(counts) <= 32:
            payload["reason_counts"] = {
                code: count for code, count in counts.items() if _is_code(code) and _is_count(count)
            }
        frames = getattr(record, "safe_traceback", None)
        if isinstance(frames, (tuple, list)):
            safe_frames = [
                frame for frame in frames[-20:]
                if isinstance(frame, str) and len(frame) <= 240 and _FRAME.fullmatch(frame)
            ]
            if safe_frames:
                payload["traceback"] = safe_frames
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO", file_path: str = "logs/app.jsonl") -> None:
    """Configure application-namespace JSON logs and bounded rotating file output."""
    # Uvicorn's default access format includes the request target; OAuth codes
    # and caller-supplied query values must not land in operational logs.
    logging.getLogger("uvicorn.access").disabled = True
    app_logger = logging.getLogger("app")
    app_logger.propagate = False
    for handler in app_logger.handlers[:]:
        app_logger.removeHandler(handler)
        handler.close()
    app_logger.setLevel(level.upper())
    formatter = JsonFormatter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    app_logger.addHandler(stream)
    if file_path.strip():
        destination = Path(file_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            destination, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        app_logger.addHandler(rotating)


def log_event(
    logger: logging.Logger,
    event: str,
    outcome: str,
    *,
    level: int = logging.INFO,
    **safe_fields: Any,
) -> None:
    """Emit a structured event; never accept free-form payload/message fields."""
    logger.log(
        level,
        "structured_event",
        extra={
            "event": event,
            "outcome": outcome,
            "request_id": request_id_var.get(),
            **{key: value for key, value in safe_fields.items() if key in _EXTRA_FIELDS},
        },
    )


def safe_traceback(exc: BaseException) -> list[str]:
    """Return frame locations only, excluding exception messages and locals."""
    locations = []
    current = exc.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        path = Path(code.co_filename)
        source = (
            path.relative_to(_PROJECT_ROOT).as_posix()
            if path.is_relative_to(_PROJECT_ROOT) else path.name
        )
        locations.append(f"{source}:{current.tb_lineno} in {code.co_name}")
        current = current.tb_next
    return locations[-20:]


def current_request_id() -> str:
    """Return the correlation ID for the current async request."""
    return request_id_var.get()


def assign_request_id() -> tuple[str, contextvars.Token[str]]:
    """Create a server-generated correlation ID; client-provided values are ignored."""
    request_id = uuid4().hex
    return request_id, request_id_var.set(request_id)
