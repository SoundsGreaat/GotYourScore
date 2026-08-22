"""Small text helpers shared by services and scripts."""

import re

# Runs of characters that carry no meaning in a snake_case key collapse
# into a single underscore (spaces, "&", curly apostrophes, slashes...).
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")

# Must match ScorecardItem.error_name (String(100)).
_MAX_ERROR_NAME_LENGTH = 100


def slugify_error_name(display_name: str) -> str:
    """Collapse a human-readable rule name into a snake_case key.

    Lowercases the input and turns every run of non-alphanumeric
    characters into a single underscore, so ``"Programs & Features"``
    becomes ``programs_features`` and ``"Customer’s questions"``
    becomes ``customer_s_questions``. The result is capped at 100
    characters to fit ``scorecard_items.error_name``; a name made
    entirely of punctuation falls back to ``"error"``.
    """
    slug = _NON_ALNUM_RUN.sub("_", display_name.strip().lower()).strip("_")
    if len(slug) > _MAX_ERROR_NAME_LENGTH:
        slug = slug[:_MAX_ERROR_NAME_LENGTH].rstrip("_")
    return slug or "error"


def unique_error_name(base: str, taken: set[str]) -> str:
    """Return ``base``, suffixed ``_2``, ``_3``, ... until unique.

    ``taken`` holds the error names already used inside the target
    template; the returned name is guaranteed to fit the 100-character
    column even with the numeric suffix appended.
    """
    if base not in taken:
        return base

    counter = 2
    while True:
        suffix = f"_{counter}"
        candidate = base[:_MAX_ERROR_NAME_LENGTH - len(suffix)].rstrip("_") + suffix
        if candidate not in taken:
            return candidate
        counter += 1
