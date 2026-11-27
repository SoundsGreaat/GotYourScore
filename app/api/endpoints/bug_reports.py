"""Check and confirm website bug reports without persisting raw submissions."""

import asyncio
import logging
from dataclasses import dataclass
from secrets import compare_digest, token_urlsafe
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.ai_rate_limit import (
    AiRateLimiter,
    AiRateLimitExceeded,
    reserve_ai_request,
)
from app.core.config import Settings, get_settings
from app.core.observability import log_event
from app.core.security import get_current_user
from app.db.database import AsyncSession, get_db
from app.models import User
from app.schemas.bug_reports import BugReportInput
from app.services import bug_report_service, github_issues

router = APIRouter(prefix="/bug-reports", tags=["bug-reports"])
logger = logging.getLogger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}
_PREVIEW_TTL_SECONDS = 600
_MAX_PREVIEWS = 256
_MAX_REPORT_BYTES = 96 * 1024  # Includes possible JSON escapes for Unicode.

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


class ConfirmReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


@dataclass(slots=True)
class Preview:
    token: str
    repository: str | None
    report: bug_report_service.PreparedReport | None
    expiry: asyncio.TimerHandle
    issue: github_issues.GitHubIssue | None = None


# The default deployment is single-worker. Opaque, short-lived tokens
# keep previews out of cookies, URLs and permanent storage.
_previews: dict[int, Preview] = {}
_publication_limiter = AiRateLimiter(
    max_requests=3, window_seconds=3600, max_concurrent_requests=1,
    operation="github_publication",
)


def _error(
    status: int,
    code: str,
    *,
    fields: tuple[str, ...] | list[str] = (),
    retry_after: str | None = None,
) -> JSONResponse:
    headers = dict(_NO_STORE)
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    data = {"code": code}
    if fields:
        data["fields"] = list(fields)
    return JSONResponse(data, status_code=status, headers=headers)


def _check_request(request: Request, settings: Settings) -> JSONResponse | None:
    if not settings.bug_reporting_enabled:
        return _error(404, "reporting_disabled")
    # This app does not enable cross-origin requests. A cross-origin HTML
    # form cannot set this header, and a script must pass CORS preflight.
    if (
        request.headers.get("X-Bug-Report") != "1"
        or request.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
    ):
        return _error(403, "invalid_request")
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return _error(415, "invalid_request")
    return None


async def _parse[Body: BaseModel](request: Request, model: type[Body], limit: int) -> Body:
    body = bytearray()
    async with asyncio.timeout(10):
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise ValueError("Request body too large")
            body.extend(chunk)
    return model.model_validate_json(body)


def _published(issue: github_issues.GitHubIssue) -> JSONResponse:
    return JSONResponse(
        {"number": issue.number, "url": issue.url}, status_code=201, headers=_NO_STORE,
    )


def _report_rejected(
    event: str, user_id: int, status: int, code: str, *, fields: tuple[str, ...] = (),
) -> JSONResponse:
    log_event(logger, event, "rejected", user_id=user_id, reason_code=code)
    return _error(status, code, fields=fields)


@router.post("/preview")
async def preview_report(
    request: Request, user: CurrentUser, db: DbSession, settings: AppSettings,
) -> JSONResponse:
    if error := _check_request(request, settings):
        return error
    user_id = user.id
    await db.commit()  # Do not hold the authentication read transaction while reading a body.
    try:
        report = await _parse(request, BugReportInput, _MAX_REPORT_BYTES)
    except ValidationError as exc:
        invalid = {
            item["loc"][0] for item in exc.errors(include_input=False, include_context=False)
            if item["type"] != "extra_forbidden" and len(item["loc"]) == 1
            and item["loc"][0] in BugReportInput.model_fields
        }
        return _error(422, "invalid_report", fields=sorted(invalid))
    except (TimeoutError, ValueError):
        return _error(422, "invalid_report")

    if user_id not in _previews and len(_previews) >= _MAX_PREVIEWS:
        return _report_rejected("bug_report.preview", user_id, 503, "preview_unavailable")
    try:
        lease = await reserve_ai_request(user_id)
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        return _error(429, "rate_limited", retry_after=(exc.headers or {}).get("Retry-After"))
    try:
        prepared = await bug_report_service.prepare_report(db, report)
    except bug_report_service.UnsuitableBugReportError as exc:
        return _report_rejected("bug_report.preview", user_id, 422, "needs_details", fields=exc.fields)
    except bug_report_service.BugReportPreparationError:
        log_event(
            logger, "bug_report.preview", "error",
            level=logging.WARNING, user_id=user_id, reason_code="privacy_check_failed",
        )
        return _error(503, "privacy_unavailable")
    finally:
        await lease.release()

    previous = _previews.get(user_id)
    if previous is None and len(_previews) >= _MAX_PREVIEWS:
        return _report_rejected("bug_report.preview", user_id, 503, "preview_unavailable")
    if previous is not None:
        previous.expiry.cancel()
    token = token_urlsafe(32)
    expiry = asyncio.get_running_loop().call_later(_PREVIEW_TTL_SECONDS, _previews.pop, user_id, None)
    _previews[user_id] = Preview(token, settings.BUG_REPORT_GITHUB_REPOSITORY, prepared, expiry)
    log_event(
        logger, "bug_report.preview", "success",
        user_id=user_id, reason_code="preview_ready",
    )
    return JSONResponse(
        {"title": prepared.title, "body": prepared.body, "token": token}, headers=_NO_STORE,
    )


@router.post("", status_code=201)
async def publish_report(
    request: Request, user: CurrentUser, db: DbSession, settings: AppSettings,
) -> JSONResponse:
    if error := _check_request(request, settings):
        return error
    user_id = user.id
    await db.commit()  # Release the authentication read transaction before the body or GitHub.
    try:
        confirmation = await _parse(request, ConfirmReport, 1024)
    except (TimeoutError, ValueError):
        return _report_rejected("bug_report.publish", user_id, 422, "expired_preview")

    preview = _previews.get(user_id)
    if (
        preview is None
        or not compare_digest(preview.token, confirmation.token)
        or preview.repository != settings.BUG_REPORT_GITHUB_REPOSITORY
    ):
        return _report_rejected("bug_report.publish", user_id, 409, "expired_preview")
    if preview.issue is not None:
        return _published(preview.issue)
    if preview.report is None:
        return _report_rejected("bug_report.publish", user_id, 409, "publication_uncertain")
    try:
        lease = await _publication_limiter.acquire(user_id)
    except AiRateLimitExceeded as exc:
        return _error(429, "rate_limited", retry_after=str(exc.retry_after))
    try:
        if _previews.get(user_id) is not preview:
            return _report_rejected("bug_report.publish", user_id, 409, "expired_preview")
        if preview.issue is not None:
            return _published(preview.issue)
        prepared = preview.report
        if prepared is None:
            return _report_rejected("bug_report.publish", user_id, 409, "publication_uncertain")
        # Claim before awaiting GitHub. The same preview is never posted
        # twice, even if the acknowledgement is lost.
        preview.report = None
        preview.issue = await github_issues.create_issue(
            settings, title=prepared.title, body=prepared.body,
        )
        log_event(
            logger, "bug_report.publish", "success",
            user_id=user_id, entity_id=preview.issue.number,
        )
        return _published(preview.issue)
    except github_issues.GitHubIssueError as exc:
        code = "publication_uncertain" if exc.may_have_published else "github_unavailable"
        if not exc.may_have_published and _previews.get(user_id) is preview:
            preview.expiry.cancel()
            del _previews[user_id]
        log_event(
            logger, "bug_report.publish", "uncertain" if exc.may_have_published else "error",
            level=logging.WARNING, user_id=user_id, reason_code=exc.reason_code,
            provider_status=exc.provider_status,
        )
        return _error(502, code)
    finally:
        await lease.release()
