"""system_prompts: enforce at most one active row per key.

Revision ID: c8d2e6f41a90
Revises: f3a9c1d47b2e
Create Date: 2026-08-22

Business rule: several rows per key act as versions, but at most one
may be ``is_active`` at a time. The CRUD endpoints maintain this
in-request (activating a row deactivates the others first); this
partial unique index turns it into a hard database guarantee that also
covers concurrent admin requests (the second committing transaction
fails instead of silently leaving two active versions).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d2e6f41a90"
down_revision: str | None = "f3a9c1d47b2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_system_prompts_key_active",
        "system_prompts",
        ["key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_system_prompts_key_active", table_name="system_prompts")
