"""multi-role users, template is_active, system prompts

Revision ID: f3a9c1d47b2e
Revises: 5fbd1f2d790c
Create Date: 2026-08-22 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d47b2e'
down_revision: Union[str, Sequence[str], None] = '5fbd1f2d790c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('user_roles',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'role')
    )
    # Backfill BEFORE dropping the legacy column: one row per user,
    # copying the previous single role value verbatim.
    op.execute("INSERT INTO user_roles (user_id, role) SELECT id, role FROM users")
    op.drop_column('users', 'role')

    op.add_column('scorecard_templates', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))

    op.create_table('system_prompts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_system_prompts_key'), 'system_prompts', ['key'], unique=False)


def downgrade() -> None:
    """Downgrade schema (best-effort data restoration)."""
    op.drop_index(op.f('ix_system_prompts_key'), table_name='system_prompts')
    op.drop_table('system_prompts')
    op.drop_column('scorecard_templates', 'is_active')

    # Recreate the single role column while user_roles still exists,
    # favoring the highest-privilege role per user. Temporary NOT NULL
    # default satisfies existing rows before the backfill UPDATE runs.
    op.add_column('users', sa.Column('role', sa.String(length=50), nullable=False, server_default='Support'))
    op.execute(
        """
        UPDATE users SET role = COALESCE((
            SELECT ur.role FROM user_roles ur WHERE ur.user_id = users.id
            ORDER BY CASE ur.role
                WHEN 'Admin' THEN 0
                WHEN 'Supervisor' THEN 1
                WHEN 'QA' THEN 2
                WHEN 'Support' THEN 3
                ELSE 4 END
            LIMIT 1
        ), 'Support')
        """
    )
    op.alter_column('users', 'role', server_default=None)
    op.drop_table('user_roles')
