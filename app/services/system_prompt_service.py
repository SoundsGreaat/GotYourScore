"""SystemPrompt business logic shared by the JSON API and admin pages.

Multiple rows per key act as versions; at most one row per key is
active at a time. Creating/activating a row deactivates the other
active rows of the same key (also enforced by the partial unique index
``uq_system_prompts_key_active``), so resolvers (see
``app.services.ai_service``) always read the newest active row per key
(``created_at DESC, id DESC``).

Like the scorecard service, functions flush but never commit — callers
own the transaction.
"""

import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemPrompt

# Admin form validation: 1-100 chars of lowercase letters, digits,
# underscores (matches SystemPrompt.key = String(100)).
PROMPT_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,100}$")

AUTOFAIL_DETECTION_SYSTEM_PROMPT = """\
You are deciding whether QA notes explicitly document an Autofail. Return
only a concise factual reason when the visible notes explicitly establish a
critical failure; otherwise return null. Do not infer a rule or outcome from
severity, implication, or context alone.

Accept explicit inline markers attached to a thesis, such as "(auto fail)"
or "— auto fail", and explicit statements such as "You received auto fail
for this case because ...", or copied text beginning "Autofail: ...".
A negated, hypothetical, historical, instructional, or otherwise unlinked
mention is NOT an Autofail. Do not invent a policy/rule when none is named.
When Autofail is valid, describe its factual reason separately from normal
scorecard deductions. Normal matching violations remain ordinary deductions,
including the violation that caused Autofail; do not count it twice or add
a synthetic scorecard key.
"""

# The fixed prompt slots the app actually resolves (single source of
# truth for the admin panel — the form only offers these keys). Each
# slot is one SystemPrompt key with its display metadata; the resolver
# for each key lives in ``app.services.ai_service``.
PROMPT_SLOTS = [
    {
        "key": "ai_scoring",
        "title": "AI Scoring",
        "description": (
            "System prompt used when AI detects scorecard violations in "
            "the review notes. The active scorecard rules and the JSON "
            "output contract are appended automatically — write only the "
            "role and scoring instructions."
        ),
    },
    {
        "key": "autofail_detection",
        "title": "Autofail Detection",
        "description": (
            "Independently versioned instructions for detecting an explicit "
            "Autofail reason in Notes-to-Score previews; format is handled "
            "by the application."
        ),
        "default_content": AUTOFAIL_DETECTION_SYSTEM_PROMPT,
    },
    {
        "key": "notes_refactor",
        "title": "Notes Refactoring",
        "description": (
            "System prompt used when rewriting QA notes for clarity. "
            "Receives the raw HTML notes and must return improved HTML."
        ),
    },
    {
        "key": "bf_comment_refactor",
        "title": "Bad Feedback Comment Refactoring",
        "description": (
            "System prompt used when rewriting the per-agent Bad "
            "Feedback comment for clarity and constructive tone. "
            "Receives the raw HTML comment and must return improved "
            "HTML with the verdict meaning unchanged."
        ),
    },
    {
        "key": "notes_from_score",
        "title": "Notes from Score",
        "description": (
            "System prompt used when drafting review notes from the ticked "
            "scorecard deductions. The deducted rules (display names, "
            "categories, points) are supplied automatically — write the "
            "reviewer voice, structure and tone, and demand a sanitized "
            "HTML fragment from those deductions only."
        ),
    },
]


async def deactivate_other_active(
    db_session: AsyncSession, key: str, exclude_id: int | None = None
) -> None:
    """Deactivate every other active row sharing ``key``."""
    stmt = update(SystemPrompt).where(
        SystemPrompt.key == key,
        SystemPrompt.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(SystemPrompt.id != exclude_id)
    await db_session.execute(stmt.values(is_active=False))


async def create_active_version(
    db_session: AsyncSession, key: str, content: str
) -> SystemPrompt:
    """Insert a new ACTIVE version of ``key``, demoting the previous one."""
    await deactivate_other_active(db_session, key)
    prompt = SystemPrompt(key=key, content=content, is_active=True)
    db_session.add(prompt)
    await db_session.flush()
    return prompt


async def activate_version(db_session: AsyncSession, prompt: SystemPrompt) -> None:
    """Make an existing row the single active version of its key."""
    await deactivate_other_active(db_session, prompt.key, exclude_id=prompt.id)
    prompt.is_active = True
    await db_session.flush()


async def grouped_versions(db_session: AsyncSession) -> list[dict[str, object]]:
    """All prompt rows grouped by key for the admin partial.

    Returns ``[{"key": str, "versions": [SystemPrompt, ...] newest
    first, "active": int | None}, ...]`` where ``active`` is the id of
    the key's active version (None when the key currently has none).
    """
    rows = (
        (
            await db_session.execute(
                select(SystemPrompt).order_by(
                    SystemPrompt.created_at.desc(), SystemPrompt.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[str, list[SystemPrompt]] = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)

    return [
        {
            "key": key,
            "versions": versions,
            "active": next(
                (version.id for version in versions if version.is_active), None
            ),
        }
        for key, versions in grouped.items()
    ]
