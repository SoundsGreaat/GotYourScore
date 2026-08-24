"""assignments drop case type

Assignments become a pure QA <-> Support-agent staffing table:
``specialized_case_type`` is dropped and ``support_agent_id`` becomes
NOT NULL. Specialized/Hybrid rows (NULL agent or case-typed scope)
are staffing metadata without quota weight, so the upgrade deletes
them instead of migrating.

Revision ID: 0d8bf68e041c
Revises: 0d88c284674e
Create Date: 2026-08-24 16:52:04.137143

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d8bf68e041c'
down_revision: Union[str, Sequence[str], None] = '0d88c284674e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Specialized-only rows have no support_agent_id at all — they
    # cannot survive the NOT NULL switch; Hybrid/case-typed rows no
    # longer fit the simplified model. Drop them first.
    op.execute("DELETE FROM qa_assignments WHERE specialized_case_type IS NOT NULL")
    op.drop_constraint(
        op.f('ck_qa_assignments_at_least_one_target'),
        'qa_assignments',
        type_='check',
    )
    op.alter_column(
        'qa_assignments', 'support_agent_id',
        existing_type=sa.INTEGER(), nullable=False,
    )
    op.drop_column('qa_assignments', 'specialized_case_type')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'qa_assignments',
        sa.Column('specialized_case_type', sa.VARCHAR(length=50), nullable=True),
    )
    op.alter_column(
        'qa_assignments', 'support_agent_id',
        existing_type=sa.INTEGER(), nullable=True,
    )
    op.create_check_constraint(
        op.f('ck_qa_assignments_at_least_one_target'),
        'qa_assignments',
        'num_nonnulls(support_agent_id, specialized_case_type) >= 1',
    )
