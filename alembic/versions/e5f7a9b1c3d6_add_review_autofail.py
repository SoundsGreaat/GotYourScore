"""Add the separate Autofail outcome for QA Score reviews.

Revision ID: e5f7a9b1c3d6
Revises: ba32b7e244a0
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f7a9b1c3d6"
down_revision = "ba32b7e244a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reviews",
        sa.Column("autofail", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("reviews", sa.Column("autofail_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_reviews_autofail",
        "reviews",
        """
        (NOT autofail AND autofail_reason IS NULL)
        OR (autofail AND case_type <> 'No Cases'
            AND autofail_reason IS NOT NULL AND autofail_reason ~ '[^[:space:]]'
            AND (status <> 'completed' OR final_score IS NOT DISTINCT FROM 0))
        """,
    )


def downgrade() -> None:
    op.drop_constraint("ck_reviews_autofail", "reviews", type_="check")
    op.drop_column("reviews", "autofail_reason")
    op.drop_column("reviews", "autofail")
