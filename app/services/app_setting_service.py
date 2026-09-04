"""AppSetting business logic shared by the admin pages and resolvers.

One row per key (unique); functions flush but never commit — callers
own the transaction, mirroring ``system_prompt_service``.

Validation of the OpenRouter provider payload lives here so the admin
endpoint stays thin: the body must be a JSON object whose keys are
restricted to OpenRouter's provider-routing options (see the official
Provider Routing docs) with matching types. An empty object is allowed
— it means "let OpenRouter decide everything".
"""

import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

# AppSetting keys managed by the Admin AI panel.
OPENROUTER_PROVIDER_KEY = "openrouter_provider"
OPENROUTER_REQUEST_KEY = "openrouter_request"

# OpenRouter's normalized reasoning-effort values. An empty admin value means
# no ``reasoning`` object is sent, letting the selected model use its default.
REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

_STRING_LIST_KEYS = ("order", "only", "ignore", "quantizations")
_BOOLEAN_KEYS = ("allow_fallbacks", "require_parameters", "zdr",
                 "enforce_distillable_text")

_ALLOWED_KEYS = frozenset(
    (*_STRING_LIST_KEYS, *_BOOLEAN_KEYS, "data_collection", "sort",
     "preferred_min_throughput", "preferred_max_latency", "max_price")
)

_SORT_BY_VALUES = ("latency", "price", "throughput")
_SORT_PARTITION_VALUES = ("model", "none")
_DATA_COLLECTION_VALUES = ("allow", "deny")
# Percentile cutoffs accepted inside performance-threshold objects.
_PERCENTILE_KEYS = frozenset(("p50", "p75", "p90", "p99"))


def _is_number(value: object) -> bool:
    # bool is an int subclass — a JSON true/false is NOT a valid number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_string_list(key: str, value: object) -> None:
    if not isinstance(value, list) or not all(
        isinstance(entry, str) for entry in value
    ):
        raise ValueError(f"{key!r} must be a list of strings.")


def _validate_percentiles(key: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{key!r} object form must be a JSON object.")
    bad = [k for k in value if k not in _PERCENTILE_KEYS]
    if bad:
        allowed = ", ".join(sorted(_PERCENTILE_KEYS))
        raise ValueError(
            f"{key!r} has invalid percentile key {bad[0]!r}. "
            f"Allowed: {allowed}."
        )
    for percentile_key, threshold in value.items():
        if not _is_number(threshold):
            raise ValueError(
                f"{key!r}.{percentile_key} must be a number."
            )


def _validate_threshold(key: str, value: object) -> None:
    if _is_number(value):
        return
    _validate_percentiles(key, value)


def _validate_sort(value: object) -> None:
    if isinstance(value, str):
        if value not in _SORT_BY_VALUES:
            raise ValueError(
                f"'sort' must be one of: {', '.join(_SORT_BY_VALUES)}."
            )
        return

    if not isinstance(value, dict):
        raise ValueError(
            "'sort' must be a string or an object with a 'by' field."
        )

    unknown = [k for k in value if k not in ("by", "partition")]
    if unknown:
        raise ValueError(f"'sort' has unknown key {unknown[0]!r}.")

    if value.get("by") not in _SORT_BY_VALUES:
        raise ValueError(
            f"'sort.by' must be one of: {', '.join(_SORT_BY_VALUES)}."
        )

    partition = value.get("partition")
    if partition is not None and partition not in _SORT_PARTITION_VALUES:
        raise ValueError(
            "'sort.partition' must be one of: "
            f"{', '.join(_SORT_PARTITION_VALUES)}."
        )


def _validate_entry(key: str, value: object) -> None:
    """Check one payload entry against its documented type/values."""
    if key == "sort":
        _validate_sort(value)
        return

    if key == "data_collection":
        if value not in _DATA_COLLECTION_VALUES:
            raise ValueError(
                "'data_collection' must be one of: "
                f"{', '.join(_DATA_COLLECTION_VALUES)}."
            )
        return

    if key in ("preferred_min_throughput", "preferred_max_latency"):
        _validate_threshold(key, value)
        return

    if key == "max_price":
        # Deliberately shallow: any nested price object is accepted.
        if not isinstance(value, dict):
            raise ValueError("'max_price' must be a JSON object.")
        return

    if key in _STRING_LIST_KEYS:
        _validate_string_list(key, value)
        return

    if not isinstance(value, bool):
        raise ValueError(f"{key!r} must be a boolean.")


def parse_openrouter_provider(raw: str) -> dict:
    """Validate a provider-routing JSON payload; raise ValueError if bad."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON: {exc.msg}.") from None

    if not isinstance(parsed, dict):
        raise ValueError("The payload must be a JSON object.")

    unknown = [key for key in parsed if key not in _ALLOWED_KEYS]
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS))
        raise ValueError(
            f"Unknown key {unknown[0]!r}. Allowed keys: {allowed}."
        )

    for key, value in parsed.items():
        _validate_entry(key, value)

    return parsed


def parse_openrouter_request(model_raw: str, reasoning_effort_raw: str) -> dict:
    """Validate optional model/reasoning overrides from the Admin form.

    Both fields are deliberately optional: an empty form means no stored
    override and preserves the application's built-in request defaults.
    """
    model = model_raw.strip()
    reasoning_effort = reasoning_effort_raw.strip()

    if model:
        if len(model) > 200:
            raise ValueError("Model identifier must be 200 characters or fewer.")
        if not model.isascii() or not all(
            char.isalnum() or char in "._:/-" for char in model
        ):
            raise ValueError(
                "Model identifier may contain only letters, digits, ., _, :, / and -."
            )

    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(
            "Reasoning effort must be one of: "
            f"{', '.join(REASONING_EFFORTS)}."
        )

    value: dict[str, str] = {}
    if model:
        value["model"] = model
    if reasoning_effort:
        value["reasoning_effort"] = reasoning_effort
    return value


async def get_value(db_session: AsyncSession, key: str) -> dict | None:
    """Return the stored JSONB value for ``key``, or None when absent."""
    return await db_session.scalar(
        select(AppSetting.value).where(AppSetting.key == key)
    )


async def upsert(
    db_session: AsyncSession, key: str, value: dict
) -> AppSetting:
    """Insert or update the single row for ``key`` (flush, no commit)."""
    setting = (
        await db_session.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if setting is None:
        setting = AppSetting(key=key, value=value)
        db_session.add(setting)
    else:
        setting.value = value
    await db_session.flush()
    return setting


async def delete_key(db_session: AsyncSession, key: str) -> bool:
    """Drop the row for ``key``; True when a row was removed."""
    result = await db_session.execute(
        delete(AppSetting).where(AppSetting.key == key)
    )
    return bool(result.rowcount)
