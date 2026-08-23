"""reviews: status, assigned_qa_id, created_by, deleted_at.

Revision ID: bf4cdc2a7efb
Revises: d7e4b9a3f1c2
Create Date: 2026-08-23 17:15:50.990015

Additive-only change to ``reviews``:

- ``status`` VARCHAR(20) NOT NULL DEFAULT 'completed' — lifecycle flag
  ('pending' delegated handoffs vs 'completed' scored reviews); the
  server default keeps every pre-existing row completed.
- ``assigned_qa_id`` INTEGER NULL → users.id ON DELETE SET NULL
  (indexed) — the QA a pending review was delegated to.
- ``created_by`` INTEGER NULL → users.id ON DELETE SET NULL — who
  opened the row (audit; never rewritten).
- ``deleted_at`` TIMESTAMPTZ NULL — soft-delete timestamp.

Backfill: none needed — the status server default classifies all
existing rows as 'completed', which matches their semantics exactly.
The unrelated partial index autogenerate wanted to drop on
``system_prompts`` (``uq_system_prompts_key_active``) is a known
metadata blind spot and is deliberately NOT touched here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bf4cdc2a7efb"
down_revision: str | None = "d7e4b9a3f1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reviews",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
    )
    op.add_column(
        "reviews",
        sa.Column("assigned_qa_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reviews",
        sa.Column("created_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reviews",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_reviews_assigned_qa_id"),
        "reviews",
        ["assigned_qa_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_reviews_assigned_qa_id_users",
        "reviews",
        "users",
        ["assigned_qa_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_reviews_created_by_users",
        "reviews",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_reviews_created_by_users", "reviews", type_="foreignkey")
    op.drop_constraint(
        "fk_reviews_assigned_qa_id_users", "reviews", type_="foreignkey"
    )
    op.drop_index(op.f("ix_reviews_assigned_qa_id"), table_name="reviews")
    op.drop_column("reviews", "deleted_at")
    op.drop_column("reviews", "created_by")
    op.drop_column("reviews", "assigned_qa_id")
    op.drop_column("reviews", "status")
