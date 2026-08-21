"""init

Revision ID: 097cefdc0816
Revises:
Create Date: 2026-08-21 18:38:56.395287

Initial schema for GotYourScore: users, qa_assignments, reviews.

Handwritten (no reachable PostgreSQL for ``alembic revision --autogenerate``);
content is compiled from the model metadata (Base.metadata) so it matches
the models exactly. Run ``uv run alembic upgrade head`` once a database
is provisioned.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '097cefdc0816'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    op.create_table(
        'qa_assignments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('qa_id', sa.Integer(), nullable=False),
        sa.Column('support_agent_id', sa.Integer(), nullable=True),
        sa.Column('specialized_case_type', sa.String(length=50), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['qa_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['support_agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            'num_nonnulls(support_agent_id, specialized_case_type) = 1',
            name='ck_qa_assignments_exactly_one_target',
        ),
    )
    op.create_index('ix_qa_assignments_qa_id', 'qa_assignments', ['qa_id'])
    op.create_index(
        'ix_qa_assignments_support_agent_id', 'qa_assignments', ['support_agent_id']
    )

    op.create_table(
        'reviews',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('support_agent_id', sa.Integer(), nullable=False),
        sa.Column('qa_id', sa.Integer(), nullable=False),
        sa.Column('case_type', sa.String(length=50), nullable=False),
        sa.Column('scorecard_data', postgresql.JSONB(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('final_score', sa.Float(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['support_agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['qa_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_reviews_support_agent_id', 'reviews', ['support_agent_id'])
    op.create_index('ix_reviews_qa_id', 'reviews', ['qa_id'])
    op.create_index('ix_reviews_created_at', 'reviews', ['created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_reviews_created_at', table_name='reviews')
    op.drop_index('ix_reviews_qa_id', table_name='reviews')
    op.drop_index('ix_reviews_support_agent_id', table_name='reviews')
    op.drop_table('reviews')

    op.drop_index('ix_qa_assignments_support_agent_id', table_name='qa_assignments')
    op.drop_index('ix_qa_assignments_qa_id', table_name='qa_assignments')
    op.drop_table('qa_assignments')

    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
