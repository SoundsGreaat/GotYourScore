"""Scorecard rule snapshot + template administration service.

Historical immutability rule: every saved review embeds a snapshot of
the rules that were ACTIVE at save time inside
``reviews.scorecard_data`` (``rules_snapshot``). Later edits,
deactivations or template swaps must never alter past reviews' scores
or breakdowns — scoring always reads the stored snapshot-era values,
never the live rules. This service is the single source of the
snapshot; both review-creation endpoints share it.

The same module also hosts the admin-panel template management logic
(list / load / bulk-save / create / toggle) so the scorecard rules stay
in one place. Service functions flush but NEVER commit: callers own the
transaction (this also lets scripts run them inside a rolled-back
transaction for testing).
"""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import slugify_error_name, unique_error_name
from app.models import CaseTypeEnum, ScorecardItem, ScorecardTemplate

# Keep in sync with ScorecardItem.category (String(200)) and
# ScorecardTemplate.name (String(255)) / display_name (String(255)).
_MAX_CATEGORY_LENGTH = 200
_MAX_NAME_LENGTH = 255

# Upper bound for penalty points; mirrors ai_service.MAX_DEDUCTION so an
# item can never ask the model for more than the sanitizer allows.
_MAX_PENALTY_POINTS = 100


async def get_active_rules(
    case_type: CaseTypeEnum, db_session: AsyncSession
) -> dict[str, object]:
    """Return the snapshot pieces for the currently active rules.

    Only items of ACTIVE templates whose case type matches are
    included. When several active templates define the same
    ``error_name``, the first one wins (templates ordered by id,
    mirroring the AI prompt builder's dedup).

    Returns:
        ``{"case_type": "<value>", "template_ids": [...],
        "items": [{"error_name", "display_name", "penalty_points",
        "category"}, ...]}`` — stored verbatim as ``rules_snapshot``.
        ``category`` is additive; snapshots written before it existed
        simply lack the key and remain valid.
    """
    stmt = (
        select(ScorecardItem)
        .join(
            ScorecardTemplate,
            ScorecardItem.template_id == ScorecardTemplate.id,
        )
        .where(
            ScorecardTemplate.case_type == case_type,
            ScorecardTemplate.is_active.is_(True),
            ScorecardItem.is_active.is_(True),
        )
        .order_by(
            ScorecardTemplate.id,
            ScorecardItem.error_name,
        )
    )

    items = list((await db_session.execute(stmt)).scalars().all())

    template_ids: list[int] = []
    snapshot_items: list[dict[str, object]] = []
    seen: set[str] = set()

    for item in items:
        if item.template_id not in template_ids:
            template_ids.append(item.template_id)

        # Dedup by error_name ALONE (first template id wins — rows are
        # ordered by template id): the per-template unique constraint
        # already guarantees uniqueness inside one template, so keying
        # on (template_id, error_name) would keep cross-template
        # duplicates that the documented contract forbids.
        if item.error_name in seen:
            continue
        seen.add(item.error_name)

        snapshot_items.append(
            {
                "error_name": item.error_name,
                "display_name": item.display_name,
                "penalty_points": int(item.penalty_points),
                "category": item.category,
            }
        )

    return {
        "case_type": case_type.value,
        "template_ids": template_ids,
        "items": snapshot_items,
    }


# ---------------------------------------------------------------------------
# Template administration (admin panel)
# ---------------------------------------------------------------------------


def _clean_display_name(raw: object, *, row_label: str) -> str:
    """Validate and normalize a rule display name."""
    display_name = str(raw or "").strip()
    if not display_name:
        raise ValueError(f"{row_label}: the rule name cannot be empty.")
    if len(display_name) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"{row_label}: the rule name is too long "
            f"(max {_MAX_NAME_LENGTH} characters)."
        )
    return display_name


def _clean_category(raw: object, *, row_label: str) -> str:
    """Validate and normalize a category label ('General' fallback)."""
    category = str(raw or "").strip() or "General"
    if len(category) > _MAX_CATEGORY_LENGTH:
        raise ValueError(
            f"{row_label}: the category is too long "
            f"(max {_MAX_CATEGORY_LENGTH} characters)."
        )
    return category


def _validate_penalty(raw: object, *, row_label: str) -> int:
    """Coerce a penalty value to a sane non-negative integer."""
    try:
        penalty = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{row_label}: penalty points must be a whole number, got {raw!r}."
        ) from None
    if penalty < 0 or penalty > _MAX_PENALTY_POINTS:
        raise ValueError(
            f"{row_label}: penalty points must be between 0 and "
            f"{_MAX_PENALTY_POINTS}."
        )
    return penalty


def _coerce_case_type(value: object) -> CaseTypeEnum:
    """Convert a raw value to CaseTypeEnum or raise ValueError.

    Accepts enum members as-is and their string VALUES ("Initial Fix");
    note ``str(member)`` is "CaseTypeEnum.MEMBER" on Python 3.12, so
    members must be matched before any str() conversion.
    """
    if isinstance(value, CaseTypeEnum):
        return value
    try:
        return CaseTypeEnum(str(value))
    except ValueError:
        valid = ", ".join(case_type.value for case_type in CaseTypeEnum)
        raise ValueError(f"Unknown case type {str(value)!r}. Valid: {valid}.") from None


async def list_templates(
    db_session: AsyncSession,
) -> list[tuple[ScorecardTemplate, int]]:
    """All templates with their item counts, ordered by id.

    One grouped query — no N+1.
    """
    stmt = (
        select(ScorecardTemplate, func.count(ScorecardItem.id))
        .outerjoin(ScorecardItem, ScorecardItem.template_id == ScorecardTemplate.id)
        .group_by(ScorecardTemplate.id)
        .order_by(ScorecardTemplate.id)
    )
    rows = (await db_session.execute(stmt)).all()
    return [(template, int(item_count)) for template, item_count in rows]


async def load_editor(
    template_id: int, db_session: AsyncSession
) -> tuple[ScorecardTemplate | None, list[dict[str, object]]]:
    """Load a template plus its items grouped by category for editing.

    Returns ``(template, groups)`` where ``groups`` is
    ``[{"category": str, "items": [ScorecardItem, ...]}, ...]`` with
    categories sorted alphabetically and items sorted by
    ``display_name`` (casefold). Inactive items are included so admins
    can re-activate them. ``(None, [])`` when the template is missing.
    """
    template = await db_session.get(ScorecardTemplate, template_id)
    if template is None:
        return None, []

    items = list(
        (
            await db_session.execute(
                select(ScorecardItem).where(ScorecardItem.template_id == template_id)
            )
        )
        .scalars()
        .all()
    )
    items.sort(key=lambda item: (item.display_name.casefold(), item.display_name))

    items_by_category: dict[str, list[ScorecardItem]] = {}
    for item in items:
        items_by_category.setdefault(item.category, []).append(item)

    groups = [
        {"category": category, "items": items_by_category[category]}
        for category in sorted(items_by_category, key=str.casefold)
    ]
    return template, groups


async def create_template(
    name: str | None, case_type: object, db_session: AsyncSession
) -> ScorecardTemplate:
    """Create a new ACTIVE template (flushed, not committed)."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Template name cannot be empty.")
    if len(clean_name) > _MAX_NAME_LENGTH:
        raise ValueError(
            f"Template name is too long (max {_MAX_NAME_LENGTH} characters)."
        )

    template = ScorecardTemplate(
        name=clean_name,
        case_type=_coerce_case_type(case_type),
        is_active=True,
    )
    db_session.add(template)
    await db_session.flush()
    return template


async def toggle_template(
    template_id: int, db_session: AsyncSession
) -> ScorecardTemplate:
    """Flip a template's ``is_active`` flag (flushed, not committed).

    Business semantic: MULTIPLE active templates per case type are
    allowed — their rules merge across active templates with
    first-wins dedup (see ``get_active_rules``) — so toggling never
    deactivates siblings.
    """
    template = await db_session.get(ScorecardTemplate, template_id)
    if template is None:
        raise ValueError(f"Scorecard template {template_id} does not exist.")
    template.is_active = not template.is_active
    await db_session.flush()
    return template


async def bulk_save(
    template_id: int,
    db_session: AsyncSession,
    *,
    name: str | None = None,
    case_type: object | None = None,
    updates: list[dict[str, object]] | None = None,
    creations: list[dict[str, object]] | None = None,
    deletions: list[int] | None = None,
) -> ScorecardTemplate:
    """Apply a whole editor form to one template (flushed, not committed).

    - ``updates``: dicts ``{id, display_name, category, penalty_points,
      is_active}``. ONLY those columns change — ``error_name`` is
      IMMUTABLE for existing items because saved reviews count
      occurrences by that key; renaming a display name must never move
      the multiplier.
    - ``creations``: same shape without ``id``; the ``error_name`` key
      is generated via :func:`slugify_error_name` and made unique
      within the template (``_2``, ``_3``, ... on collision).
    - ``deletions``: item ids to remove.
    - ``name`` / ``case_type``: template-level fields, updated when
      provided (not None).

    Raises:
        ValueError: on validation problems (empty names, bad penalties,
            unknown case types, foreign ids) — callers map this to 400.
    """
    template = await db_session.get(ScorecardTemplate, template_id)
    if template is None:
        raise ValueError(f"Scorecard template {template_id} does not exist.")

    if name is not None:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("Template name cannot be empty.")
        if len(clean_name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"Template name is too long (max {_MAX_NAME_LENGTH} characters)."
            )
        template.name = clean_name
    if case_type is not None:
        template.case_type = _coerce_case_type(case_type)

    deleted_ids = set(deletions or [])
    if deleted_ids:
        await db_session.execute(
            delete(ScorecardItem).where(
                ScorecardItem.template_id == template_id,
                ScorecardItem.id.in_(deleted_ids),
            )
        )

    all_items = list(
        (
            await db_session.execute(
                select(ScorecardItem).where(ScorecardItem.template_id == template_id)
            )
        )
        .scalars()
        .all()
    )
    remaining = [item for item in all_items if item.id not in deleted_ids]
    by_id = {item.id: item for item in remaining}

    for update in updates or []:
        try:
            item_id = int(str(update["id"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("A rule row is missing a valid item id.") from None
        row_label = f"Rule #{item_id}"
        item = by_id.get(item_id)
        if item is None:
            raise ValueError(
                f"Rule {item_id} does not belong to template {template_id}"
                " (or was marked for deletion in the same save)."
            )
        item.display_name = _clean_display_name(
            update.get("display_name"), row_label=row_label
        )
        item.category = _clean_category(update.get("category"), row_label=row_label)
        item.penalty_points = _validate_penalty(
            update.get("penalty_points"), row_label=row_label
        )
        item.is_active = bool(update.get("is_active", True))
        # error_name intentionally untouched (see docstring).

    # Names still in use after deletions/updates constrain new keys.
    taken = {item.error_name for item in remaining}
    for creation in creations or []:
        row_label = "New rule"
        display_name = _clean_display_name(
            creation.get("display_name"), row_label=row_label
        )
        category = _clean_category(creation.get("category"), row_label=row_label)
        penalty_points = _validate_penalty(
            creation.get("penalty_points"), row_label=row_label
        )
        error_name = unique_error_name(slugify_error_name(display_name), taken)
        taken.add(error_name)
        db_session.add(
            ScorecardItem(
                template_id=template_id,
                error_name=error_name,
                display_name=display_name,
                category=category,
                penalty_points=penalty_points,
                is_active=bool(creation.get("is_active", True)),
            )
        )

    await db_session.flush()
    return template
