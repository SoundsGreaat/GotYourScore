"""scorecard_items: add position column.

Revision ID: d7e4b9a3f1c2
Revises: b4f8e2a6c9d1
Create Date: 2026-08-23

Additive-only change: every scoring rule gains an admin-defined
ordering key (0-based) used by the editor, the AI prompt builder and
the rules snapshot embedded into newly saved reviews. Existing rows
are backfilled with one deterministic statement that preserves the
previously visible order (categories alphabetical, items alphabetical
within a category); no other data is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e4b9a3f1c2"
down_revision: str | None = "b4f8e2a6c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scorecard_items",
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        """
        UPDATE scorecard_items AS si
        SET position = ranked.rn - 1
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY template_id
                       ORDER BY LOWER(category), LOWER(display_name), id
                   ) AS rn
            FROM scorecard_items
        ) AS ranked
        WHERE si.id = ranked.id
        """
    )


def downgrade() -> None:
    op.drop_column("scorecard_items", "position")
