"""Safe structured application logging and request correlation."""

from __future__ import annotations

import contextvars
import json
import logging
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
request_failure_logged: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "request_failure_logged", default=False
)


class JsonFormatter(logging.Formatter):
    """Serialize only explicitly-safe structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "event": getattr(record, "event", "application.log"),
            "outcome": getattr(record, "outcome", "info"),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for name in (
            "method",
            "route",
            "status_code",
            "user_id",
            "entity_id",
            "error_type",
            "provider_status",
            "reason_code",
            "elapsed_ms",
            "count",
            "skipped_count",
            "operation",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        frames = getattr(record, "safe_traceback", None)
        if frames:
            payload["traceback"] = frames
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO", file_path: str = "logs/app.jsonl") -> None:
    """Configure application-namespace JSON logs and bounded rotating file output."""
    # Uvicorn's default access format includes the request target; OAuth codes
    # and caller-supplied query values must not land in operational logs.
    logging.getLogger("uvicorn.access").disabled = True
    app_logger = logging.getLogger("app")
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
    if level >= logging.WARNING:
        request_failure_logged.set(True)
    allowed = {
        key: value
        for key, value in safe_fields.items()
        if key
        in {
            "method",
            "route",
            "status_code",
            "user_id",
            "entity_id",
            "error_type",
            "provider_status",
            "reason_code",
            "elapsed_ms",
            "count",
            "skipped_count",
            "operation",
            "safe_traceback",
        }
    }
    logger.log(
        level,
        "structured_event",
        extra={
            "event": event,
            "outcome": outcome,
            "request_id": request_id_var.get(),
            **allowed,
        },
    )


def safe_traceback(exc: BaseException) -> list[str]:
    """Return frame locations only, excluding exception messages and locals."""
    return [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    ][-20:]


def current_request_id() -> str:
    """Return the correlation ID for the current async request."""
    return request_id_var.get()


def assign_request_id() -> tuple[str, contextvars.Token[str]]:
    """Create a server-generated correlation ID; client-provided values are ignored."""
    request_id = uuid4().hex
    return request_id, request_id_var.set(request_id)
