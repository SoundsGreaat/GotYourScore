"""Soft-delete reviews whose case_type was removed from the enum.

Revision ID: d4e6f8a0b2c4
Revises: c3d5e7f90a1b
Create Date: 2026-08-25

'Scheduled Fix' was removed from CaseTypeEnum. SQLAlchemy validates
case_type on load (SAEnum validate_strings), so any surviving row with
the removed value 500s every query that hydrates it. Soft-deleting the
rows hides them from all surfaces (active_filter) while preserving the
data; there is no schema change (case_type is a plain VARCHAR).

Downgrade cannot know which rows were hidden by THIS migration versus
earlier manual deletions, so it is intentionally a no-op.
"""

from alembic import op

revision = "d4e6f8a0b2c4"
down_revision = "c3d5e7f90a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE reviews
        SET deleted_at = now()
        WHERE case_type = 'Scheduled Fix'
          AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Deliberate no-op: restoring rows removed together with an enum
    # value would break loading again. Restore manually if ever needed.
    pass
