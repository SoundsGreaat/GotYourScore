"""Rename the Refund Check date without touching Bad Feedback.

Revision ID: ba32b7e244a0
Revises: a1b2c3d4e5f6
Create Date: 2026-11-23
"""

from alembic import op


revision = "ba32b7e244a0"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("refund_checks", "fb_date", new_column_name="r_date")
    op.execute("ALTER INDEX ix_refund_checks_fb_date RENAME TO ix_refund_checks_r_date")
    op.execute(
        "UPDATE tracker_fields SET key = 'r_date' "
        "WHERE tracker = 'refund-check' AND key = 'fb_date'"
    )


def downgrade() -> None:
    op.alter_column("refund_checks", "r_date", new_column_name="fb_date")
    op.execute("ALTER INDEX ix_refund_checks_r_date RENAME TO ix_refund_checks_fb_date")
    op.execute(
        "UPDATE tracker_fields SET key = 'fb_date' "
        "WHERE tracker = 'refund-check' AND key = 'r_date'"
    )
