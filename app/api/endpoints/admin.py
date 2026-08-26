"""Admin panel pages (Jinja2/HTMX) — Admin-only.

Server-rendered fragments swapped into ``templates/admin.html`` by
HTMX; every response is an HTML PARTIAL, never JSON. Auth follows the
pages-router pattern (``app.api.endpoints.pages``): unauthenticated
visitors get a 303 to /login, authenticated NON-admin users a 303 to
"/". Every route is additionally guarded by
``current_user.has_role(RoleEnum.ADMIN)`` inside the dependency.

Prompt versioning semantics are shared with the JSON CRUD API
(``app.api.endpoints.system_prompts``) through
``app.services.system_prompt_service``; scorecard template logic lives
in ``app.services.scorecard_service``.

Error convention for HTMX forms: validation failures return HTTP 400
whose body is ONLY a small daisyUI alert fragment
(``<div role="alert" class="alert alert-error"><span>MESSAGE</span></div>``)
that the frontend swaps into an ``#editor-alert`` target.
"""

from datetime import datetime, timezone
from typing import Annotated

import markupsafe
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.endpoints.pages import (
    _htmx_redirect_if_needed,
    _session_user,
    templates,
)
from app.db.database import get_db
from app.models import (
    CaseTypeEnum,
    Review,
    RoleEnum,
    ScorecardTemplate,
    SystemPrompt,
    User,
    UserRole,
)
from app.services import (
    ai_service,
    app_setting_service,
    scorecard_service,
    system_prompt_service,
    user_service,
)

router = APIRouter(tags=["admin"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# Valid role values for the roles form (RoleEnum VALUES as strings).
_ROLE_VALUES = [role.value for role in RoleEnum]


async def get_admin_user_or_redirect(
    request: Request,
    db: DbSession,
) -> User | RedirectResponse:
    """Pages-style auth dependency for the admin panel.

    Unauthenticated -> 303 /login; authenticated users WITHOUT the
    Admin role -> 303 "/" (the dashboard hides the admin chrome from
    them anyway). Admins pass through untouched.
    """
    user = await _session_user(request, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not user.has_role(RoleEnum.ADMIN):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return user


# Dependency alias: the authenticated Admin, or a RedirectResponse the
# handler must return verbatim before doing any work.
AdminPageUser = Annotated[User | RedirectResponse, Depends(get_admin_user_or_redirect)]


def _guard(auth: object, request: Request) -> Response | None:
    """Return the redirect the handler must honor, if any."""
    return _htmx_redirect_if_needed(auth, request)


def _not_found() -> PlainTextResponse:
    """Bare 404 for missing rows (HTMX surfaces the status code)."""
    return PlainTextResponse("Not Found", status_code=status.HTTP_404_NOT_FOUND)


def _error_alert(message: str) -> Response:
    """400 response whose body is ONLY a daisyUI error alert fragment."""
    content = (
        '<div role="alert" class="alert alert-error">'
        f"<span>{markupsafe.escape(message)}</span></div>"
    )
    return Response(
        content=content,
        status_code=status.HTTP_400_BAD_REQUEST,
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# Panel shell
# ---------------------------------------------------------------------------


@router.get("/admin", name="admin_panel", summary="Admin panel page")
async def admin_page(request: Request, auth: AdminPageUser) -> Response:
    """Render the admin panel shell; tabs lazy-load the partials."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"current_user": auth},
    )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


async def _prompts_context(
    db: AsyncSession, *, error: str | None = None, saved: bool = False
) -> dict[str, object]:
    """Context for the prompts partial (shared by GET and POST flows).

    Fixed slots (``system_prompt_service.PROMPT_SLOTS``) enriched with
    each key's version history: ``versions`` newest first and
    ``active_id`` = the active version's id (None when the key has no
    active row — resolvers then fall back to the hardcoded constants).
    """
    grouped = {
        group["key"]: group
        for group in await system_prompt_service.grouped_versions(db)
    }
    return {
        "slots": [
            {
                "key": slot["key"],
                "title": slot["title"],
                "description": slot["description"],
                "versions": grouped.get(slot["key"], {}).get("versions", []),
                "active_id": grouped.get(slot["key"], {}).get("active"),
            }
            for slot in system_prompt_service.PROMPT_SLOTS
        ],
        "error": error,
        "saved": saved,
    }


@router.get(
    "/admin/partials/prompts",
    name="admin_partial_prompts",
    summary="System prompts partial",
    include_in_schema=False,
)
async def partial_prompts(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """All prompt versions grouped by key, newest first."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_prompts.html",
        context=await _prompts_context(db),
    )


@router.post("/admin/prompts", name="admin_create_prompt")
async def create_prompt(
    request: Request,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Create a new ACTIVE prompt version (demotes the previous one).

    Reads the urlencoded form directly (HTMX default) — no
    ``python-multipart`` dependency needed. The key must be one of the
    fixed slots; anything else is a 400 alert fragment.
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    form = await request.form()
    key = str(form.get("key", "")).strip()
    content = str(form.get("content", ""))

    slot_keys = {slot["key"] for slot in system_prompt_service.PROMPT_SLOTS}
    if key not in slot_keys:
        return _error_alert(f"Unknown prompt slot {key!r}.")

    error: str | None = None
    if not content.strip():
        error = "Prompt content cannot be empty."

    saved = False
    if error is None:
        await system_prompt_service.create_active_version(db, key, content)
        await db.commit()
        saved = True

    return templates.TemplateResponse(
        request=request,
        name="partials/admin_prompts.html",
        context=await _prompts_context(db, error=error, saved=saved),
    )


@router.post("/admin/prompts/{prompt_id}/activate", name="admin_activate_prompt")
async def activate_prompt(
    request: Request,
    prompt_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Activate one version; its siblings of the same key deactivate."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    prompt = await db.get(SystemPrompt, prompt_id)
    if prompt is None:
        return _not_found()

    await system_prompt_service.activate_version(db, prompt)
    await db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_prompts.html",
        context=await _prompts_context(db),
    )


@router.post("/admin/prompts/{prompt_id}/delete", name="admin_delete_prompt")
async def delete_prompt(
    request: Request,
    prompt_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Delete one prompt row (the key may end up with no active row;
    resolvers then fall back to their hardcoded constants)."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    prompt = await db.get(SystemPrompt, prompt_id)
    if prompt is None:
        return _not_found()

    await db.delete(prompt)
    await db.commit()
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_prompts.html",
        context=await _prompts_context(db),
    )


# ---------------------------------------------------------------------------
# Scorecard templates
# ---------------------------------------------------------------------------


async def _scorecards_context(
    db: AsyncSession,
    *,
    selected_id: int | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """Context for the scorecards list partial."""
    pairs = await scorecard_service.list_templates(db)
    return {
        "templates": [
            {
                "id": template.id,
                "name": template.name,
                "case_type": template.case_type,
                "is_active": template.is_active,
                "created_at": template.created_at,
                "item_count": item_count,
            }
            for template, item_count in pairs
        ],
        "case_types": list(CaseTypeEnum),
        "selected_id": selected_id,
        "error": error,
    }


def _render_scorecards(
    request: Request, context: dict[str, object]
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_scorecards.html",
        context=context,
    )


@router.get(
    "/admin/partials/scorecards",
    name="admin_partial_scorecards",
    summary="Scorecard templates partial",
    include_in_schema=False,
)
async def partial_scorecards(
    request: Request,
    auth: AdminPageUser,
    db: DbSession,
    selected: int | None = Query(default=None),
) -> Response:
    """All templates with item counts; ``?selected=<id>`` highlights one."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return _render_scorecards(
        request, await _scorecards_context(db, selected_id=selected)
    )


async def _editor_context(
    db: AsyncSession,
    template_id: int,
    *,
    error: str | None = None,
    saved: bool = False,
) -> tuple[Response | None, dict[str, object]]:
    """Build editor context; the Response is a 404 when the id is unknown."""
    template, groups = await scorecard_service.load_editor(template_id, db)
    if template is None:
        return _not_found(), {}
    return None, {
        "t": template,
        "groups": groups,
        "case_types": list(CaseTypeEnum),
        "error": error,
        "saved": saved,
    }


@router.get(
    "/admin/partials/scorecard-editor/{template_id}",
    name="admin_partial_scorecard_editor",
    summary="Scorecard editor partial",
    include_in_schema=False,
)
async def partial_scorecard_editor(
    request: Request,
    template_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """One template's rules grouped by category, ready to bulk-edit."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    not_found, context = await _editor_context(db, template_id)
    if not_found is not None:
        return not_found
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_scorecard_editor.html",
        context=context,
    )


@router.post("/admin/scorecards/create", name="admin_create_scorecard")
async def create_scorecard(
    request: Request,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Create an ACTIVE template; re-render the list with it selected."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    form = await request.form()
    try:
        template = await scorecard_service.create_template(
            str(form.get("name", "")), str(form.get("case_type", "")), db
        )
        await db.commit()
    except ValueError as exc:
        return _render_scorecards(
            request,
            await _scorecards_context(db, selected_id=None, error=str(exc)),
        )

    return _render_scorecards(
        request, await _scorecards_context(db, selected_id=template.id)
    )


def _parse_bulk_form(form: object) -> tuple[list[dict], list[dict], list[int]]:
    """Decode the indexed editor form into bulk_save arguments.

    Rows are numbered from 0; a row exists while ``display_name_{i}``
    is present. ``item_id_{i}`` empty means a NEW row. ``active_{i}``
    is a checkbox (present = active). ``removed_ids`` is a hidden
    comma-separated list of ids the UI marked for deletion. The row
    index doubles as the admin-defined ordering key: the frontend
    submits rows in DOM order, so it is stored as ``position``.
    """
    updates: list[dict] = []
    creations: list[dict] = []
    index = 0
    while f"display_name_{index}" in form:
        raw_id = str(form.get(f"item_id_{index}", "")).strip()
        row = {
            "display_name": str(form.get(f"display_name_{index}", "")),
            "category": str(form.get(f"category_{index}", "")),
            "penalty_points": str(form.get(f"penalty_{index}", "")).strip(),
            "is_active": f"active_{index}" in form,
            "position": index,
        }
        if raw_id:
            try:
                row["id"] = int(raw_id)
            except ValueError:
                raise ValueError(
                    f"Row {index + 1}: invalid rule id {raw_id!r}."
                ) from None
            updates.append(row)
        else:
            creations.append(row)
        index += 1

    deletions: list[int] = []
    for chunk in str(form.get("removed_ids", "")).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            deletions.append(int(chunk))
        except ValueError:
            raise ValueError(f"Invalid removed rule id {chunk!r}.") from None

    return updates, creations, deletions


@router.post("/admin/scorecards/{template_id}/save", name="admin_save_scorecard")
async def save_scorecard(
    request: Request,
    template_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Bulk-save the editor form; re-render the editor partial.

    Validation failures return 400 whose body is ONLY the alert
    fragment for the frontend's ``#editor-alert`` swap target.
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    if await db.get(ScorecardTemplate, template_id) is None:
        return _not_found()

    form = await request.form()
    try:
        updates, creations, deletions = _parse_bulk_form(form)
        await scorecard_service.bulk_save(
            template_id,
            db,
            name=str(form.get("name", "")),
            case_type=str(form.get("case_type", "")),
            updates=updates,
            creations=creations,
            deletions=deletions,
        )
        await db.commit()
    except ValueError as exc:
        return _error_alert(str(exc))

    not_found, context = await _editor_context(db, template_id, saved=True)
    if not_found is not None:
        return not_found
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_scorecard_editor.html",
        context=context,
    )


@router.post("/admin/scorecards/{template_id}/toggle", name="admin_toggle_scorecard")
async def toggle_scorecard(
    request: Request,
    template_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Flip a template's active flag; re-render the list partial.

    ONE active template per case type: turning a template ON demotes
    its active siblings of the same case type (see
    ``scorecard_service.toggle_template``).
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    if await db.get(ScorecardTemplate, template_id) is None:
        return _not_found()

    try:
        template = await scorecard_service.toggle_template(template_id, db)
        await db.commit()
    except ValueError as exc:
        return _render_scorecards(
            request,
            await _scorecards_context(db, selected_id=template_id, error=str(exc)),
        )

    return _render_scorecards(
        request, await _scorecards_context(db, selected_id=template.id)
    )


# ---------------------------------------------------------------------------
# AI provider routing
# ---------------------------------------------------------------------------


async def _ai_context(
    db: AsyncSession, *, error: str | None = None, saved: bool = False
) -> dict[str, object]:
    """Context for the AI settings partial (shared by GET and POST flows).

    ``stored`` is the DB row's value or None; when None the hardcoded
    ``ai_service.OPENROUTER_PROVIDER`` constant is in effect.
    """
    stored = await app_setting_service.get_value(
        db, app_setting_service.OPENROUTER_PROVIDER_KEY
    )
    return {
        "stored": stored,
        "default": ai_service.OPENROUTER_PROVIDER,
        "error": error,
        "saved": saved,
    }


@router.get(
    "/admin/partials/ai",
    name="admin_partial_ai",
    summary="AI settings partial",
    include_in_schema=False,
)
async def partial_ai(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Current OpenRouter provider routing + its JSON edit form."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_ai.html",
        context=await _ai_context(db),
    )


@router.post("/admin/ai/provider", name="admin_save_ai_provider")
async def save_ai_provider(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Upsert the ``openrouter_provider`` AppSetting from a JSON textarea.

    Validation failures return 400 whose body is ONLY the alert
    fragment for the frontend's ``#editor-alert`` swap target.
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    form = await request.form()
    try:
        value = app_setting_service.parse_openrouter_provider(
            str(form.get("provider", ""))
        )
    except ValueError as exc:
        return _error_alert(str(exc))

    await app_setting_service.upsert(
        db, app_setting_service.OPENROUTER_PROVIDER_KEY, value
    )
    await db.commit()

    return templates.TemplateResponse(
        request=request,
        name="partials/admin_ai.html",
        context=await _ai_context(db, saved=True),
    )


@router.post("/admin/ai/provider/reset", name="admin_reset_ai_provider")
async def reset_ai_provider(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Delete the stored row so the hardcoded default applies again."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    await app_setting_service.delete_key(
        db, app_setting_service.OPENROUTER_PROVIDER_KEY
    )
    await db.commit()

    return templates.TemplateResponse(
        request=request,
        name="partials/admin_ai.html",
        context=await _ai_context(db),
    )


# ---------------------------------------------------------------------------
# Users & roles management
# ---------------------------------------------------------------------------


def _display_name(user: User) -> str:
    """Row label for a user: real name when known, nickname otherwise.

    Placeholder accounts are created with ``name=None``; the nickname
    (capitalized email local part) is what the team calls them until
    their first Google login syncs the real name.
    """
    return user.name or user.nickname


def _user_row(user: User) -> dict[str, object]:
    """Common user-card shape: user + display name + canonical roles."""
    return {
        "u": user,
        "name": _display_name(user),
        # Canonical RoleEnum definition order, whatever the storage order.
        "roles": [role for role in RoleEnum if role in user.roles],
    }


async def _users_context(
    db: AsyncSession,
    *,
    current_user: User,
    status_message: str | None = None,
) -> dict[str, object]:
    """Context for the merged users partial: ACTIVE users only.

    ``current_user_id`` drives the "(you)" badge and the disabled
    own-row Admin checkbox in the role cards.
    """
    users = list(
        (
            await db.execute(select(User).where(User.active_filter()))
        ).scalars().all()
    )
    rows = [_user_row(user) for user in users]
    rows.sort(key=lambda row: str(row["name"]).casefold())
    return {
        "rows": rows,
        "current_user_id": current_user.id,
        "role_choices": _ROLE_VALUES,
        "default_role": RoleEnum.SUPPORT.value,
        "status": status_message,
    }


def _render_users(request: Request, context: dict[str, object]) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_users.html",
        context=context,
    )


@router.get(
    "/admin/partials/users",
    name="admin_partial_users",
    summary="Admin-managed users partial",
    include_in_schema=False,
)
async def partial_users(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Active users: add-user form, search box, per-user role cards."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return _render_users(request, await _users_context(db, current_user=auth))


@router.post("/admin/users/{user_id}/roles", name="admin_update_roles")
async def update_user_roles(
    request: Request,
    user_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Atomically replace a user's roles from repeated ``roles`` checkboxes."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    target = await db.get(User, user_id)
    if target is None:
        return _not_found()

    form = await request.form()
    values = list(dict.fromkeys(str(value) for value in form.getlist("roles")))

    if not values:
        return _error_alert("Select at least one role.")
    unknown = [value for value in values if value not in _ROLE_VALUES]
    if unknown:
        return _error_alert(
            f"Unknown role(s): {', '.join(unknown)}."
        )
    if target.id == auth.id and RoleEnum.ADMIN.value not in values:
        return _error_alert("You cannot revoke your own Admin access.")

    # Atomic replace: delete + inserts commit together (or not at all).
    await db.execute(delete(UserRole).where(UserRole.user_id == target.id))
    for value in values:
        db.add(UserRole(user_id=target.id, role=RoleEnum(value)))
    await db.commit()

    # Refresh the identity-map instance so the partial shows fresh roles.
    await db.execute(
        select(User)
        .where(User.id == target.id)
        .execution_options(populate_existing=True)
    )

    return _render_users(
        request,
        await _users_context(
            db, current_user=auth, status_message=f"Roles updated for {target.nickname}."
        ),
    )


@router.post("/admin/users/create", name="admin_create_user")
async def create_user(
    request: Request,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Create a placeholder account from a nickname (+ role checkboxes).

    Validation failures return 400 whose body is ONLY the alert
    fragment for the frontend's ``#users-alert`` swap slot. With no
    roles checked the account defaults to Support. ``users.name`` stays
    NULL — the person's first Google login fills it in.
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    form = await request.form()
    values = list(dict.fromkeys(str(value) for value in form.getlist("roles")))

    unknown = [value for value in values if value not in _ROLE_VALUES]
    if unknown:
        return _error_alert(f"Unknown role(s): {', '.join(unknown)}.")

    try:
        nickname = user_service.normalize_nickname(str(form.get("nickname", "")))
    except ValueError as exc:
        return _error_alert(str(exc))

    email = user_service.placeholder_email(nickname)
    if await user_service.is_email_taken(email, db):
        return _error_alert(f"A user with email {email} already exists.")

    db.add(
        User(
            email=email,
            name=None,
            roles=[RoleEnum(value) for value in values] or [RoleEnum.SUPPORT],
        )
    )
    await db.commit()

    return _render_users(
        request,
        await _users_context(
            db, current_user=auth, status_message=f"User {nickname} created."
        ),
    )


@router.post("/admin/users/{user_id}/delete", name="admin_delete_user")
async def delete_user(
    request: Request,
    user_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Soft-delete a user (``deleted_at`` = now).

    Historical reviews stay intact and keep resolving the user's name;
    the account disappears from new-work surfaces and Google login.
    Guards: you cannot delete yourself, and the last active Admin is
    irreplaceable — both surface as alert fragments.
    """
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    target = await db.get(User, user_id)
    if target is None:
        return _not_found()

    if target.id == auth.id:
        return _error_alert("You cannot delete your own account.")

    active_admins = await db.scalar(
        select(func.count())
        .select_from(User)
        .join(UserRole, User.id == UserRole.user_id)
        .where(User.active_filter(), UserRole.role == RoleEnum.ADMIN)
    )
    if target.has_role(RoleEnum.ADMIN) and (active_admins or 0) <= 1:
        return _error_alert("You cannot delete the last active Admin.")

    target.deleted_at = datetime.now(timezone.utc)
    await db.commit()

    return _render_users(
        request, await _users_context(db, current_user=auth)
    )


async def _deleted_users_context(db: AsyncSession) -> dict[str, object]:
    """Context for the deleted-users partial, newest deletion first."""
    users = list(
        (
            await db.execute(
                select(User)
                .where(User.deleted_at.is_not(None))
                .order_by(User.deleted_at.desc())
            )
        ).scalars().all()
    )
    rows = [
        {**_user_row(user), "deleted_at": user.deleted_at} for user in users
    ]
    return {"rows": rows}


async def _render_deleted_users(request: Request, db: AsyncSession) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_deleted_users.html",
        context=await _deleted_users_context(db),
    )


@router.get(
    "/admin/partials/deleted-users",
    name="admin_partial_deleted_users",
    summary="Deleted users partial",
    include_in_schema=False,
)
async def partial_deleted_users(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Every soft-deleted user, newest deletion first."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return await _render_deleted_users(request, db)


@router.post("/admin/users/{user_id}/restore", name="admin_restore_user")
async def restore_user(
    request: Request,
    user_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Clear ``deleted_at`` (undo the soft delete) and re-render the
    deleted-users partial — same fragment-refresh pattern as the
    review restore flow."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    target = await db.get(User, user_id)
    if target is None:
        return _not_found()

    if target.is_deleted:
        target.deleted_at = None
        await db.commit()

    return await _render_deleted_users(request, db)


# ---------------------------------------------------------------------------
# Deleted reviews (soft-delete restore)
# ---------------------------------------------------------------------------


async def _deleted_reviews_context(db: AsyncSession) -> dict[str, object]:
    """Context for the deleted-reviews partial.

    Soft-deleted reviews only (``deleted_at`` NOT NULL), newest deletion
    first, with agent/reviewer/creator display names materialized in
    Python (``User.nickname`` is a computed property, not a column).
    """
    reviews = list(
        (
            await db.execute(
                select(Review)
                .where(Review.deleted_at.is_not(None))
                .order_by(Review.deleted_at.desc())
            )
        ).scalars().all()
    )
    person_ids = {
        rid
        for review in reviews
        for rid in (review.support_agent_id, review.qa_id, review.created_by)
        if rid is not None
    }
    nicknames: dict[int, str] = {}
    if person_ids:
        people = (
            await db.execute(select(User).where(User.id.in_(person_ids)))
        ).scalars().all()
        nicknames = {person.id: person.nickname for person in people}

    rows = [
        {
            "id": review.id,
            "case_type": review.case_type,
            "case_number": review.case_number,
            "final_score": review.final_score,
            "created_at": review.created_at,
            "deleted_at": review.deleted_at,
            "agent_name": nicknames.get(review.support_agent_id, "Unknown"),
            "reviewer_name": nicknames.get(review.qa_id, "Unknown"),
            "creator_name": nicknames.get(
                review.created_by, nicknames.get(review.qa_id, "Unknown")
            ),
        }
        for review in reviews
    ]
    return {"rows": rows}


async def _render_deleted_reviews(
    request: Request, db: AsyncSession
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/admin_deleted_reviews.html",
        context=await _deleted_reviews_context(db),
    )


@router.get(
    "/admin/partials/deleted-reviews",
    name="admin_partial_deleted_reviews",
    summary="Deleted reviews partial",
    include_in_schema=False,
)
async def partial_deleted_reviews(
    request: Request, auth: AdminPageUser, db: DbSession
) -> Response:
    """Every soft-deleted review, newest deletion first."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect
    return await _render_deleted_reviews(request, db)


@router.post(
    "/admin/reviews/{review_id}/restore",
    name="admin_restore_review",
)
async def restore_review(
    request: Request,
    review_id: int,
    auth: AdminPageUser,
    db: DbSession,
) -> Response:
    """Clear ``deleted_at`` (undo the soft delete) and re-render the
    deleted-reviews partial — same fragment-refresh pattern as the
    prompt delete flow."""
    redirect = _guard(auth, request)
    if redirect is not None:
        return redirect

    review = await db.get(Review, review_id)
    if review is None:
        return _not_found()

    if review.deleted_at is not None:
        await db.execute(
            update(Review).where(Review.id == review_id).values(deleted_at=None)
        )
        await db.commit()

    return await _render_deleted_reviews(request, db)
