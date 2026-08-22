"""scorecard_items: add category column.

Revision ID: b4f8e2a6c9d1
Revises: c8d2e6f41a90
Create Date: 2026-08-22

Additive-only change: every scoring rule gains a free-form taxonomy
label (``"Regular Optimization Steps"``, ...) shown as a section header
in the AI scoring prompt and used to group rules in the admin editor.
Existing rows are backfilled with ``'General'`` via the server default;
no data is touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4f8e2a6c9d1"
down_revision: str | None = "c8d2e6f41a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scorecard_items",
        sa.Column(
            "category",
            sa.String(length=200),
            nullable=False,
            server_default="General",
        ),
    )


def downgrade() -> None:
    op.drop_column("scorecard_items", "category")
