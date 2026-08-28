"""bad_feedback completed_at

Revision ID: 18956f3413e2
Revises: 615b6b3d8d0c
Create Date: 2026-08-28 23:35:17.324565

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '18956f3413e2'
down_revision: Union[str, Sequence[str], None] = '615b6b3d8d0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add bad_feedbacks.completed_at (hand-written: autogenerate would
    also emit the pre-existing qa_assignments drift)."""
    op.add_column(
        "bad_feedbacks",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove bad_feedbacks.completed_at."""
    op.drop_column("bad_feedbacks", "completed_at")
