"""Allow integer and decimal tracker field definitions."""

from alembic import op

revision = "34c81bd2a9f7"
down_revision = "f3c84a9de170"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_tracker_fields_type", "tracker_fields", type_="check")
    op.create_check_constraint(
        "ck_tracker_fields_type",
        "tracker_fields",
        "field_type IN ('date', 'decimal', 'integer', 'text', 'textarea')",
    )


def downgrade() -> None:
    # The previous constraint rejects existing numeric definitions. Convert
    # those definitions explicitly before downgrading to preserve their values.
    op.drop_constraint("ck_tracker_fields_type", "tracker_fields", type_="check")
    op.create_check_constraint(
        "ck_tracker_fields_type",
        "tracker_fields",
        "field_type IN ('date', 'text', 'textarea')",
    )
