"""Core security: Google OAuth client, current-user dependency, RBAC."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from authlib.integrations.starlette_client import OAuth

from app.core.config import get_settings
from app.db.database import AsyncSession, get_db
from app.models import RoleEnum, User

# Session key under which the authenticated user's id is stored.
SESSION_USER_ID_KEY = "user_id"


@lru_cache
def get_oauth() -> OAuth:
    """Return the OAuth registry with the Google client registered.

    Created lazily (and cached) so that importing this module never
    crashes when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset —
    registration with ``None`` credentials would raise immediately.
    """
    settings = get_settings()
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
    )
    return oauth


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI dependency: resolve the logged-in User from the session.

    Raises 401 when there is no session or the user no longer exists.
    """
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
        )
    return user


class RoleChecker:
    """FastAPI dependency restricting access to the given roles.

    Usage::

        @router.get(
            "/admin-only",
            dependencies=[Depends(RoleChecker([RoleEnum.ADMIN]))],
        )
        async def endpoint(...): ...
    """

    def __init__(self, allowed_roles: list[RoleEnum]) -> None:
        self.allowed_roles = allowed_roles

    async def __call__(self, current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user
