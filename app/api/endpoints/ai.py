"""AI assistant API endpoints.

- POST /api/ai/refactor: rewrite QA notes (HTML) for clarity, grammar
  and professional tone while preserving markup and images.
- POST /api/ai/score: preview AI scoring of QA notes against the case
  type's configured scorecard rules — returns the raw deductions and
  a multiplier-free base score; nothing is persisted (saving goes
  through POST /api/reviews/auto-score).

All routes require an authenticated user with the QA, Supervisor or
Admin role.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import RoleEnum, User
from app.schemas.ai import RefactorIn, RefactorOut, ScoreIn, ScoreOut
from app.services import ai_service

router = APIRouter(prefix="/ai", tags=["ai"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# RoleChecker returns the authenticated User, so the handler receives it.
AiUser = Annotated[
    User,
    Depends(RoleChecker([RoleEnum.QA, RoleEnum.SUPERVISOR, RoleEnum.ADMIN])),
]


@router.post("/refactor", response_model=RefactorOut)
async def refactor_notes(
    payload: RefactorIn,
    _current_user: AiUser,
) -> RefactorOut:
    """Rewrite QA notes (HTML) without persisting anything.

    - 503 when ``OPENROUTER_API_KEY`` is not configured.
    - 502 when the AI call fails or returns an empty response.
    """
    try:
        html = await ai_service.refactor_qa_notes(payload.html)
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
