"""User management helpers for admin-managed accounts.

Placeholder-identity design: admins create users by providing ONLY a
nickname. The account starts with no name and a synthesized email
``{nickname_lower}@<ALLOWED_DOMAIN>``; the real name is filled in on
the person's first Google login, when
:func:`app.api.auth.resolve_google_user` syncs Google's display name.

Emails are permanent identities and are NEVER reused: uniqueness is
checked across ALL users, including soft-deleted ones.
"""

import re

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import User

# Nicknames: 1-50 chars of lowercase letters, digits, dots, underscores
# or hyphens — i.e. exactly what a valid email local part may contain
# for our synthesized addresses.
_NICKNAME_RE = re.compile(r"^[a-z0-9._-]{1,50}$")


def normalize_nickname(raw: str) -> str:
    """Strip, lowercase and validate an admin-provided nickname.

    Raises ``ValueError`` when the result is not 1-50 chars of
    ``[a-z0-9._-]`` — anything else would produce an invalid or ugly
    placeholder email.
    """
    nickname = raw.strip().lower()
    if not _NICKNAME_RE.fullmatch(nickname):
        raise ValueError(
            "Nickname must be 1-50 characters using only letters, "
            "digits, dots, underscores or hyphens"
        )
    return nickname


def placeholder_email(nickname: str) -> str:
    """Build the placeholder email for a normalized nickname.

    Uses ``settings.ALLOWED_DOMAIN`` so the address matches the OAuth
    domain restriction.
    """
    domain = get_settings().ALLOWED_DOMAIN.lower().lstrip("@")
    return f"{nickname}@{domain}"


async def is_email_taken(email: str, db_session: AsyncSession) -> bool:
    """True when ANY user already holds this email.

    Deliberately ignores soft delete: emails are never recycled, so a
    soft-deleted user's address still blocks new registrations.
    """
    stmt = select(exists().where(User.email == email.lower()))
    return bool((await db_session.execute(stmt)).scalar())
