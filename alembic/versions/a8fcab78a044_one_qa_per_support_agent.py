"""one QA per support agent

A Support agent is staffed to AT MOST ONE QA. The upgrade first
resolves existing duplicates (an agent listed under several QAs) by
keeping only the OLDEST row per agent — ``(created_at, id)`` tuple
comparison, so same-timestamp ties fall back to insert order — then a
UNIQUE index on ``support_agent_id`` enforces the invariant at the DB
level.

Revision ID: a8fcab78a044
Revises: 0d8bf68e041c
Create Date: 2026-08-24 17:26:30.512422

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a8fcab78a044'
down_revision: Union[str, Sequence[str], None] = '0d8bf68e041c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Keep the oldest assignment per agent; drop the newer duplicates.
    op.execute(
        "DELETE FROM qa_assignments a"
        " USING qa_assignments b"
        " WHERE a.support_agent_id = b.support_agent_id"
        " AND (b.created_at, b.id) < (a.created_at, a.id)"
    )
    op.drop_index(
        op.f('ix_qa_assignments_support_agent_id'),
        table_name='qa_assignments',
    )
    op.create_index(
        op.f('ix_qa_assignments_support_agent_id'),
        'qa_assignments',
        ['support_agent_id'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_qa_assignments_support_agent_id'),
        table_name='qa_assignments',
    )
    op.create_index(
        op.f('ix_qa_assignments_support_agent_id'),
        'qa_assignments',
        ['support_agent_id'],
        unique=False,
    )
