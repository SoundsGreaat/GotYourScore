"""AI assistant API endpoints.

- POST /api/ai/refactor: rewrite QA notes (HTML) for clarity, grammar
  and professional tone while preserving markup and images.
- POST /api/ai/refactor/stream: NDJSON streaming variant of /refactor
  (same pipeline; deltas as they are generated).
- POST /api/ai/refactor-comment: same pipeline for per-agent Bad
  Feedback comments (own ``bf_comment_refactor`` system-prompt slot).
- POST /api/ai/refactor-comment/stream: NDJSON streaming variant.
- POST /api/ai/score: preview AI scoring of QA notes against the case
  type's configured scorecard rules — returns the raw deductions and
  a multiplier-free base score; nothing is persisted (saving goes
  through POST /api/reviews/auto-score).
- POST /api/ai/notes-from-score: draft the review notes (a sanitized
  HTML fragment) that justify an already-ticked raw scorecard; nothing
  is persisted.
- POST /api/ai/notes-from-score/stream: NDJSON streaming variant.

All routes require an authenticated user with the QA, Supervisor or
Admin role.

Streaming contract (the ``/stream`` variants): the response is
``application/x-ndjson`` — one JSON object per line:
- ``{"d": "..."}`` — a raw content delta, verbatim from the model;
- ``{"final": "..."}`` — terminal success event carrying the fully
  post-processed fragment (identical post-processing to the buffered
  endpoint's return value); the client MUST sanitize it before
  insertion, same trust boundary;
- ``{"error": "..."}`` — terminal failure event. HTTP status is
  already 200 once streaming starts, so failures (including ones that
  would map to 502 on the buffered routes) travel as error events.
  Only the pre-flight checks (auth, 503 when ``OPENROUTER_API_KEY``
  is unset) produce real HTTP error responses.
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import CaseTypeEnum, RoleEnum, User
from app.schemas.ai import (
    NotesFromScoreIn,
    NotesFromScoreOut,
    RefactorIn,
    RefactorOut,
    ScoreIn,
    ScoreOut,
)
from app.services import ai_service

router = APIRouter(prefix="/ai", tags=["ai"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# RoleChecker returns the authenticated User, so the handler receives it.
AiUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]

_NO_API_KEY_DETAIL = (
    "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
    "in the environment or the .env file to enable AI features."
)


def _ndjson_response(
    events: AsyncIterator[tuple[str, str]],
) -> StreamingResponse:
    """Wrap a service event stream (see the streaming contract) as NDJSON.

    ``events`` yields ``("d", delta)`` / ``("final", html)`` pairs;
    ``AnalyzeError`` (and any unexpected failure) becomes a terminal
    ``{"error": ...}`` line, since the HTTP status is already sent.
    """
    async def event_stream() -> AsyncIterator[str]:
        try:
            async for kind, value in events:
                yield json.dumps({kind: value}, ensure_ascii=False) + "\n"
        except ai_service.AnalyzeError as exc:
            yield json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n"
        except Exception as exc:  # status is already 200 — nothing else to do
            yield json.dumps(
                {"error": f"AI stream failed: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/refactor", response_model=RefactorOut)
async def refactor_notes(
    payload: RefactorIn,
    db: DbSession,
    _current_user: AiUser,
) -> RefactorOut:
    """Rewrite QA notes (HTML) without persisting anything.

    The system prompt is the newest ACTIVE ``notes_refactor``
    SystemPrompt row (hardcoded fallback when none exists).

    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI call fails or returns an empty response.
    """
    try:
        html = await ai_service.refactor_qa_notes(payload.html, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "in the environment or the .env file to enable AI features."
            ),
        ) from exc
    except ai_service.AnalyzeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI refactoring failed: {exc}",
        ) from exc
    return RefactorOut(html=html)


@router.post("/refactor/stream")
async def refactor_notes_stream(
    payload: RefactorIn,
    db: DbSession,
    _current_user: AiUser,
) -> StreamingResponse:
    """Stream a QA-notes rewrite as NDJSON (see the streaming contract).

    Same prompt resolution and post-processing as POST /refactor; the
    deltas arrive as the model generates them.
    """
    try:
        # Eager 503 contract check: client creation only touches config,
        # so it can fail BEFORE any byte is streamed.
        ai_service.get_openrouter_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_NO_API_KEY_DETAIL,
        ) from exc
    return _ndjson_response(ai_service.stream_refactor_qa_notes(payload.html, db))


@router.post("/refactor-comment", response_model=RefactorOut)
async def refactor_bf_comment(
    payload: RefactorIn,
    db: DbSession,
    _current_user: AiUser,
) -> RefactorOut:
    """Rewrite a per-agent Bad Feedback comment (HTML), unpersisted.

    The system prompt is the newest ACTIVE ``bf_comment_refactor``
    SystemPrompt row (hardcoded fallback when none exists); the slot is
    editable in the admin panel like the other AI prompts.

    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI call fails or returns an empty response.
    """
    try:
        html = await ai_service.refactor_bf_comment(payload.html, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "in the environment or the .env file to enable AI features."
            ),
        ) from exc
    except ai_service.AnalyzeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI refactoring failed: {exc}",
        ) from exc
    return RefactorOut(html=html)


@router.post("/refactor-comment/stream")
async def refactor_bf_comment_stream(
    payload: RefactorIn,
    db: DbSession,
    _current_user: AiUser,
) -> StreamingResponse:
    """Stream a Bad Feedback comment rewrite as NDJSON (see contract).

    Same prompt resolution and post-processing as POST
    /refactor-comment.
    """
    try:
        ai_service.get_openrouter_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_NO_API_KEY_DETAIL,
        ) from exc
    return _ndjson_response(
        ai_service.stream_refactor_bf_comment(payload.html, db)
    )


@router.post("/score", response_model=ScoreOut)
async def score_notes(
    payload: ScoreIn,
    db: DbSession,
    _current_user: AiUser,
) -> ScoreOut:
    """Preview-score QA notes (HTML) without persisting anything.

    The notes are scored by the OpenRouter-hosted model against the
    scorecard rules configured for ``case_type`` (generic fallback
    rules when none are configured). The response contains the raw
    deductions and a multiplier-free preview score; progressive
    multipliers apply only when a review is saved via
    POST /api/reviews/auto-score with a support agent.

    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI analysis call or response parsing fails.
    """
    try:
        scorecard = await ai_service.analyze_support_ticket(
            payload.html, payload.case_type, db
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "in the environment or the .env file to enable AI features."
            ),
        ) from exc
    except ai_service.AnalyzeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI analysis failed: {exc}",
        ) from exc

    total_deduction = sum(scorecard.values())
    return ScoreOut(
        scorecard=scorecard,
        total_deduction=total_deduction,
        base_score=100,
        final_score=max(0, 100 - total_deduction),
    )


@router.post("/notes-from-score", response_model=NotesFromScoreOut)
async def notes_from_score(
    payload: NotesFromScoreIn,
    db: DbSession,
    _current_user: AiUser,
) -> NotesFromScoreOut:
    """Draft review notes (HTML) from ticked scorecard deductions.

    The deducted rules are rendered with the case type's ACTIVE
    scorecard rules (human-readable display names, categories and the
    deducted points); deduction keys unknown to those rules are skipped.
    When ``support_agent_id`` is sent, progressive multipliers and the
    final score are computed for that agent (same engine as saving a
    review) so the notes justify the real number. The system prompt is
    the newest ACTIVE ``notes_from_score`` SystemPrompt row (hardcoded
    fallback when none exists). The response fragment is not sanitized
    server-side — the client sanitizes it with DOMPurify before
    insertion (same trust boundary as /refactor).

    - 400 when ``case_type`` is 'No Cases' (no scorecard to reference).
    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI call fails or returns an empty response.
    """
    if payload.case_type is CaseTypeEnum.NO_CASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "case_type 'No Cases' has no scorecard to draft notes from."
            ),
        )
    try:
        notes_html = await ai_service.draft_notes_from_score(
            payload.case_type,
            payload.raw_scorecard,
            db,
            support_agent_id=payload.support_agent_id,
            exclude_review_id=payload.exclude_review_id,
            no_multiplier_keys=payload.no_multiplier_keys,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "OpenRouter API key is not configured. Set OPENROUTER_API_KEY "
                "in the environment or the .env file to enable AI features."
            ),
        ) from exc
    except ai_service.AnalyzeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI note drafting failed: {exc}",
        ) from exc
    return NotesFromScoreOut(notes_html=notes_html)


@router.post("/notes-from-score/stream")
async def notes_from_score_stream(
    payload: NotesFromScoreIn,
    db: DbSession,
    _current_user: AiUser,
) -> StreamingResponse:
    """Stream draft review notes as NDJSON (see the streaming contract).

    Same rule/multiplier context and post-processing as POST
    /notes-from-score.
    """
    if payload.case_type is CaseTypeEnum.NO_CASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "case_type 'No Cases' has no scorecard to draft notes from."
            ),
        )
    try:
        ai_service.get_openrouter_client()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_NO_API_KEY_DETAIL,
        ) from exc
    return _ndjson_response(
        ai_service.stream_draft_notes_from_score(
            payload.case_type,
            payload.raw_scorecard,
            db,
            support_agent_id=payload.support_agent_id,
            exclude_review_id=payload.exclude_review_id,
            no_multiplier_keys=payload.no_multiplier_keys,
        )
    )
