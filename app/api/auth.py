"""Google OAuth 2.0 authentication endpoints."""

import logging
from typing import Annotated

from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import SESSION_USER_ID_KEY, get_current_user, get_oauth
from app.db.database import AsyncSession, get_db
from app.models import RoleEnum, User
from app.schemas import UserRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", summary="Redirect the user to Google's consent screen")
async def auth_login(request: Request) -> RedirectResponse:
    """Start the OAuth 2.0 authorization-code flow with Google."""
    oauth = get_oauth()
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", summary="Handle Google's OAuth response")
async def auth_callback(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> RedirectResponse:
    """Exchange the auth code, enforce the allowed domain, auto-register."""
    settings = get_settings()
    oauth = get_oauth()

    # Token exchange can fail (user denied consent, lost session state,
    # network errors). Surface a clean redirect instead of a raw 500.
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OAuth token exchange failed: %s", exc)
        request.session.clear()
        return RedirectResponse(
            url="/login?error=oauth_failed",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    userinfo = token.get("userinfo")
    if userinfo is None:
        # authlib >= 1.5 signature: (token, nonce, ...). Passing a falsy
        # nonce skips nonce validation (we did not send one).
        userinfo = await oauth.google.parse_id_token(token, nonce=None)

    email = userinfo.get("email")
    if not email or not userinfo.get("email_verified", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account did not return a verified email address",
        )

    email = email.lower()
    allowed_domain = settings.ALLOWED_DOMAIN.lower().lstrip("@")

    # Domain restriction: the email must end with "@<ALLOWED_DOMAIN>".
    # The "@" anchor defeats suffix tricks like user@domain.com.evil.io.
    if not email.endswith("@" + allowed_domain):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Login is restricted to @{allowed_domain} accounts",
        )

    # Auto-registration: first login creates the user with default roles.
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            name=userinfo.get("name") or email.split("@")[0],
            roles=[RoleEnum.SUPPORT],
        )
        db.add(user)
        await db.flush()

    # Commit first: if it fails, no stale user_id gets baked into the
    # session cookie.
    await db.commit()
    request.session.clear()
    request.session[SESSION_USER_ID_KEY] = user.id

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout", summary="Clear the session and return to the login page")
async def auth_logout(request: Request) -> RedirectResponse:
    """Log the user out."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me", response_model=UserRead, summary="Current authenticated user")
async def auth_me(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Return the logged-in user's profile."""
    return current_user
