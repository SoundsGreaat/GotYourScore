"""One active scorecard template per case type.

Revision ID: c3d5e7f90a1b
Revises: 8b339dd2d0a8
Create Date: 2026-08-24

Business rule change: a case type can now have only ONE active
scorecard template (activating one demotes its siblings, same as the
SystemPrompt one-active-row-per-key rule). The migration first
deactivates all but the OLDEST active template per case type, then
adds the partial unique index that enforces it at the DB level.
"""

from alembic import op

revision = "c3d5e7f90a1b"
down_revision = "8b339dd2d0a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedup guard: keep the OLDEST active row per case type, demote the rest.
    op.execute(
        """
        UPDATE scorecard_templates AS t
        SET is_active = false
        WHERE t.is_active
          AND EXISTS (
              SELECT 1
              FROM scorecard_templates AS keeper
              WHERE keeper.case_type = t.case_type
                AND keeper.is_active
                AND keeper.id < t.id
          )
        """
    )
    op.create_index(
        "uq_scorecard_templates_case_type_active",
        "scorecard_templates",
        ["case_type"],
        unique=True,
        postgresql_where="is_active",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_scorecard_templates_case_type_active", table_name="scorecard_templates"
    )
