"""users: deleted_at soft-delete column, nullable name.

Revision ID: 0d88c284674e
Revises: bf4cdc2a7efb
Create Date: 2026-08-23 20:27:44.694215

Additive-only change to ``users`` supporting admin-managed placeholder
accounts:

- ``deleted_at`` TIMESTAMPTZ NULL — soft-delete timestamp (mirrors the
  ``reviews.deleted_at`` column from bf4cdc2a7efb). Soft-deleted users
  keep their historical reviews but are hidden from new-work surfaces,
  excluded from compliance assigned-agent math, and blocked from
  Google login.
- ``name`` VARCHAR(255) NOT NULL → NULL — admin-created placeholder
  accounts store NO name; the real name is filled on the person's
  FIRST Google login by the auth name-sync. All existing rows already
  carry a name, so no backfill is needed and re-tightening in
  downgrade is safe.

The unrelated partial index autogenerate wanted to drop on
``system_prompts`` (``uq_system_prompts_key_active``) is a known
metadata blind spot and is deliberately NOT touched here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0d88c284674e"
down_revision: str | None = "bf4cdc2a7efb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "users",
        "name",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_column("users", "deleted_at")
