"""Import a scorecard CSV into one template + its items.

Input format (see ``scripts/scorecard_example.csv``):

- A CATEGORY HEADER line has a trailing comma and an empty second
  field, e.g. ``Regular Optimization Steps,`` â€” every rule below it
  inherits that category until the next header.
- An ITEM line is ``Human readable name,penalty`` (penalty 1-5 in the
  example file; anything 0-100 is accepted).
- Blank lines are skipped. Rules before the first header land in
  "General".

Each item gets ``error_name`` via ``slugify_error_name`` (unique within
the template via the ``_2``/``_3`` suffix scheme), ``is_active=true``
and the category from its header.

Usage::

    uv run python scripts/import_scorecard_csv.py \
        --file scorecard_example.csv --name "Initial Fix v2" \
        [--case-type "Initial Fix"] [--dry-run]

``--dry-run`` parses and prints what would be inserted WITHOUT
touching the database. Without it the script WRITES to whatever DB
``DATABASE_URL`` points at â€” double-check before running for real.
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path

# Allow running as a standalone script (python adds scripts/, not the
# repo root, to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.text import slugify_error_name, unique_error_name
from app.db.database import async_session_factory
from app.models import CaseTypeEnum, ScorecardItem
from app.services import scorecard_service


def parse_csv(path: Path) -> list[tuple[str, str, int]]:
    """Parse the CSV into ``(category, display_name, penalty)`` rows."""
    rows: list[tuple[str, str, int]] = []
    category = "General"

    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(csv.reader(handle), start=1):
            if not any(field.strip() for field in raw):
                continue
            name = raw[0].strip()
            second = raw[1].strip() if len(raw) > 1 else ""
            if not second:
                # Category header: trailing comma, empty second field.
                category = name or "General"
                continue
            try:
                penalty = int(second)
            except ValueError:
                raise SystemExit(
                    f"Line {line_number}: penalty {second!r} is not an integer."
                ) from None
            if penalty < 0 or penalty > 100:
                raise SystemExit(
                    f"Line {line_number}: penalty {penalty} outside 0-100."
                )
            rows.append((category, name, penalty))

    return rows


async def run(args: argparse.Namespace) -> None:
    rows = parse_csv(Path(args.file))
    if not rows:
        raise SystemExit("No item rows found in the CSV.")

    case_type = scorecard_service.coerce_case_type(args.case_type)

    taken: set[str] = set()
    prepared = []
    for category, display_name, penalty in rows:
        error_name = unique_error_name(slugify_error_name(display_name), taken)
        taken.add(error_name)
        prepared.append((category, error_name, display_name, penalty))

    print(f"Parsed {len(prepared)} rules across "
          f"{len({row[0] for row in prepared})} categories:")
    current_category = None
    for category, error_name, display_name, penalty in prepared:
        if category != current_category:
            current_category = category
            print(f"  [{category}]")
        print(f"    {display_name!r} -> {error_name} ({penalty} pts)")

    if args.dry_run:
        print("Dry run: nothing was written.")
        return

    async with async_session_factory() as session:
        template = await scorecard_service.create_template(
            args.name, case_type, session
        )
        for category, error_name, display_name, penalty in prepared:
            session.add(
                ScorecardItem(
                    template_id=template.id,
                    error_name=error_name,
                    display_name=display_name,
                    category=category,
                    penalty_points=penalty,
                    is_active=True,
                )
            )
        await session.commit()
        print(
            f"Created template #{template.id} '{template.name}' "
            f"({case_type.value}) with {len(prepared)} active items."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Path to the scorecard CSV.")
    parser.add_argument("--name", required=True, help="Name of the new template.")
    parser.add_argument(
        "--case-type",
        default="Initial Fix",
        help=f"One of: {', '.join(ct.value for ct in CaseTypeEnum)}.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse and print only; no DB writes."
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
