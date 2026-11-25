"""Add the import requirement to tracker field definitions."""

import sqlalchemy as sa
from alembic import op

revision = "f3c84a9de170"
down_revision = "e5f7a9b1c3d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tracker_fields",
        sa.Column(
            "required_for_import", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("tracker_fields", "required_for_import")
