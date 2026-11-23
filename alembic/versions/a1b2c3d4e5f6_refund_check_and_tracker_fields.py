"""Add the Refund Check tracker and configurable tracker fields.

Revision ID: a1b2c3d4e5f6
Revises: 18956f3413e2
Create Date: 2026-11-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "18956f3413e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT_FIELDS = (
    ("fb_date", "Date", "date"),
    ("source", "Source", "text"),
    ("customer_info", "Customer info", "text"),
    ("customer_feedback", "Customer feedback", "textarea"),
    ("related_case", "Related case", "text"),
)


def upgrade() -> None:
    op.add_column(
        "bad_feedbacks",
        sa.Column(
            "field_values_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.create_table(
        "refund_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fb_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("customer_info", sa.Text(), nullable=True),
        sa.Column("customer_feedback", sa.Text(), nullable=True),
        sa.Column("related_case", sa.String(length=255), nullable=True),
        sa.Column(
            "field_values_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("qa_comment", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "completed",
                name="reviewstatusenum",
                native_enum=False,
                length=20,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("assigned_qa_id", sa.Integer(), nullable=True),
        sa.Column("qa_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_qa_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["qa_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_refund_checks_assigned_qa_id"),
        "refund_checks",
        ["assigned_qa_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refund_checks_created_at"),
        "refund_checks",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refund_checks_fb_date"),
        "refund_checks",
        ["fb_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refund_checks_qa_id"),
        "refund_checks",
        ["qa_id"],
        unique=False,
    )

    op.create_table(
        "refund_check_agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "Support",
                "Sales",
                name="agentkindenum",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "fault",
            sa.Enum(
                "Full Fault",
                "Partial Fault",
                "Warning",
                "No Fault",
                name="faultenum",
                native_enum=False,
                length=20,
            ),
            nullable=True,
        ),
        sa.Column("qa_comment", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["refund_checks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_refund_check_agents_feedback_id"),
        "refund_check_agents",
        ["feedback_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_refund_check_agents_user_id"),
        "refund_check_agents",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "tracker_fields",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracker", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("field_type", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "show_in_my_reviews", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tracker IN ('bad-feedback', 'refund-check')",
            name="ck_tracker_fields_tracker",
        ),
        sa.CheckConstraint(
            "field_type IN ('date', 'text', 'textarea')",
            name="ck_tracker_fields_type",
        ),
        sa.CheckConstraint(
            "position >= 0 AND position <= 100000",
            name="ck_tracker_fields_position",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tracker", "key", name="uq_tracker_fields_tracker_key"
        ),
    )
    op.create_index(
        "ix_tracker_fields_tracker_archived_position",
        "tracker_fields",
        ["tracker", "archived_at", "position"],
        unique=False,
    )

    op.bulk_insert(
        sa.table(
            "tracker_fields",
            sa.column("tracker", sa.String()),
            sa.column("key", sa.String()),
            sa.column("label", sa.String()),
            sa.column("field_type", sa.String()),
            sa.column("position", sa.Integer()),
            sa.column("show_in_my_reviews", sa.Boolean()),
        ),
        [
            {
                "tracker": tracker,
                "key": key,
                "label": label,
                "field_type": field_type,
                "position": position,
                "show_in_my_reviews": key != "customer_info",
            }
            for tracker in ("bad-feedback", "refund-check")
            for position, (key, label, field_type) in enumerate(_DEFAULT_FIELDS)
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tracker_fields_tracker_archived_position", table_name="tracker_fields"
    )
    op.drop_table("tracker_fields")
    op.drop_index(
        op.f("ix_refund_check_agents_user_id"), table_name="refund_check_agents"
    )
    op.drop_index(
        op.f("ix_refund_check_agents_feedback_id"), table_name="refund_check_agents"
    )
    op.drop_table("refund_check_agents")
    op.drop_index(op.f("ix_refund_checks_qa_id"), table_name="refund_checks")
    op.drop_index(op.f("ix_refund_checks_fb_date"), table_name="refund_checks")
    op.drop_index(op.f("ix_refund_checks_created_at"), table_name="refund_checks")
    op.drop_index(
        op.f("ix_refund_checks_assigned_qa_id"), table_name="refund_checks"
    )
    op.drop_table("refund_checks")
    op.drop_column("bad_feedbacks", "field_values_data")
