"""Correlate and summarize HTTP requests at the ASGI boundary."""

import logging
from asyncio import CancelledError
from contextlib import suppress
from time import perf_counter
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.observability import assign_request_id, log_event, request_id_var, safe_traceback

logger = logging.getLogger(__name__)

_REJECTION_CODES = {
    400: "business_validation_error",
    401: "authentication_required",
    403: "authorization_denied",
    404: "resource_not_found",
    405: "method_not_allowed",
    409: "resource_conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    422: "request_validation_error",
    429: "rate_limited",
}
_QUIET_SUCCESS_ROUTES = {"ping", "static"}


def _route_name(scope: Scope) -> str:
    if isinstance(scope.get("endpoint"), StaticFiles):
        return "static"
    return getattr(scope.get("route"), "name", None) or "unmatched"


class RequestObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id, token = assign_request_id()
        started_at = perf_counter()
        status: int | None = None
        can_end_early = False
        finished = False
        disconnected = False
        redirected = False

        async def receive_message() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def send_message(message: Message) -> None:
            nonlocal status, can_end_early, finished, redirected
            message_type = message["type"]
            if message_type == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                can_end_early = "content-length" not in headers
                redirected = "hx-redirect" in headers
            try:
                await send(message)
            except OSError as exc:
                raise ClientDisconnect() from exc
            if message_type == "http.response.start":
                status = message["status"]
            elif message_type == "http.response.body":
                finished = not message.get("more_body", False)
            elif message_type == "http.response.pathsend":
                finished = True

        def record(event: str, outcome: str, *, level: int = logging.INFO, **fields: Any) -> None:
            log_event(
                logger, event, outcome,
                level=level,
                method=scope.get("method"),
                route=_route_name(scope),
                user_id=scope.get("state", {}).get("log_user_id"),
                status_code=status,
                elapsed_ms=round((perf_counter() - started_at) * 1000, 2),
                **fields,
            )

        try:
            await self.app(scope, receive_message, send_message)
        except ClientDisconnect:
            record("http.request.incomplete", "disconnected", reason_code="client_disconnected")
        except CancelledError:
            record("http.request.incomplete", "cancelled", reason_code="request_cancelled")
            raise
        except Exception as exc:
            # Keep exception messages and locals out of both the response and
            # the ASGI server's default error logger.
            if status is None:
                status = 500
                error = JSONResponse({"detail": "Internal server error."}, status_code=status)
                with suppress(ClientDisconnect):
                    await error(scope, receive_message, send_message)
            elif can_end_early and not finished:
                with suppress(OSError):
                    await send({"type": "http.response.body", "body": b""})
            record(
                "http.request.unhandled_error", "error",
                level=logging.ERROR, reason_code="unhandled_exception",
                error_type=type(exc).__name__, safe_traceback=safe_traceback(exc),
            )
        else:
            if not finished or status is None:
                record(
                    "http.request.incomplete",
                    "disconnected" if disconnected else "error",
                    level=logging.INFO if disconnected else logging.ERROR,
                    reason_code="client_disconnected" if disconnected else "incomplete_response",
                )
            elif status >= 500:
                record(
                    "http.request.completed", "server_failure",
                    level=logging.ERROR, reason_code="http_server_error",
                )
            elif status >= 400:
                record(
                    "http.request.completed", "expected_failure",
                    level=logging.WARNING, reason_code=_REJECTION_CODES.get(status, "http_client_error"),
                )
            elif _route_name(scope) not in _QUIET_SUCCESS_ROUTES:
                record("http.request.completed", "redirect" if redirected or status >= 300 else "success")
        finally:
            request_id_var.reset(token)
