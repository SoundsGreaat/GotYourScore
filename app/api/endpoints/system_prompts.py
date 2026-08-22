"""System prompt management API endpoints (Admin-only).

CRUD for DB-stored LLM system prompts addressed by ``key`` (e.g.
``"ai_scoring"``). Multiple rows per key act as versions; at most one
row per key should be active — creating or updating a row with
``is_active=true`` deactivates the other active rows of the same key,
so resolvers (see ``app.services.ai_service``) always read the newest
active row (created_at DESC, id DESC).

RBAC matrix: every route requires the Admin role; Supervisor, QA and
Support-only users receive 403 from the RoleChecker.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RoleChecker
from app.db.database import get_db
from app.models import RoleEnum, SystemPrompt, User
from app.schemas.system_prompt import (
    SystemPromptCreate,
    SystemPromptRead,
    SystemPromptUpdate,
)
from app.services import system_prompt_service

router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# RoleChecker returns the authenticated User, so the handler receives it.
AdminUser = Annotated[User, Depends(RoleChecker([RoleEnum.ADMIN]))]


@router.post("", response_model=SystemPromptRead, status_code=status.HTTP_201_CREATED)
async def create_system_prompt(
    payload: SystemPromptCreate,
    db: DbSession,
    _current_user: AdminUser,
) -> SystemPromptRead:
    """Create a prompt row; activating it deactivates the previous
    active row of the same key."""
    if payload.is_active:
        await system_prompt_service.deactivate_other_active(db, payload.key)

    prompt = SystemPrompt(
        key=payload.key,
        content=payload.content,
        is_active=payload.is_active,
    )
    db.add(prompt)
    await db.commit()
    await db.refresh(prompt)
    return SystemPromptRead.model_validate(prompt)


@router.get("", response_model=list[SystemPromptRead])
async def list_system_prompts(
    db: DbSession,
    _current_user: AdminUser,
    key: str | None = Query(default=None),
) -> list[SystemPromptRead]:
    """List prompt rows, newest first, optionally filtered by key."""
    stmt = select(SystemPrompt).order_by(
        SystemPrompt.created_at.desc(), SystemPrompt.id.desc()
    )
    if key is not None:
        stmt = stmt.where(SystemPrompt.key == key)
    rows = (await db.execute(stmt)).scalars().all()
    return [SystemPromptRead.model_validate(row) for row in rows]


@router.get("/{prompt_id}", response_model=SystemPromptRead)
async def get_system_prompt(
    prompt_id: int,
    db: DbSession,
    _current_user: AdminUser,
) -> SystemPromptRead:
    """Fetch a single prompt row by id (404 if missing)."""
    prompt = await db.get(SystemPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt {prompt_id} not found.",
        )
    return SystemPromptRead.model_validate(prompt)


@router.api_route(
    "/{prompt_id}",
    methods=["PUT", "PATCH"],
    response_model=SystemPromptRead,
)
async def update_system_prompt(
    prompt_id: int,
    payload: SystemPromptUpdate,
    db: DbSession,
    _current_user: AdminUser,
) -> SystemPromptRead:
    """Update content/is_active (partial semantics); activating
    deactivates the other active rows of the same key."""
    prompt = await db.get(SystemPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt {prompt_id} not found.",
        )

    data = payload.model_dump(exclude_unset=True)
    if "content" in data:
        prompt.content = data["content"]
    if "is_active" in data:
        if data["is_active"]:
            await system_prompt_service.deactivate_other_active(
                db, prompt.key, exclude_id=prompt.id
            )
        prompt.is_active = data["is_active"]

    await db.commit()
    await db.refresh(prompt)
    return SystemPromptRead.model_validate(prompt)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_system_prompt(
    prompt_id: int,
    db: DbSession,
    _current_user: AdminUser,
) -> None:
    """Delete a prompt row (404 if missing)."""
    prompt = await db.get(SystemPrompt, prompt_id)
    if prompt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt {prompt_id} not found.",
        )
    await db.delete(prompt)
    await db.commit()
