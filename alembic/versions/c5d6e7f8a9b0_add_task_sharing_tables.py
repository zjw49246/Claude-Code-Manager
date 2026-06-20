"""add task sharing tables

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Create Date: 2026-06-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared_to_open_id", sa.String(100), nullable=False),
        sa.Column("shared_to_name", sa.String(100), nullable=True),
        sa.Column("shared_to_ccm_url", sa.String(500), nullable=False),
        sa.Column("share_token", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "shared_to_open_id", name="uq_task_share_recipient"),
    )
    op.create_index("ix_task_shares_task_id", "task_shares", ["task_id"])

    op.create_table(
        "project_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shared_to_open_id", sa.String(100), nullable=False),
        sa.Column("shared_to_name", sa.String(100), nullable=True),
        sa.Column("shared_to_ccm_url", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "shared_to_open_id", name="uq_project_share_recipient"),
    )
    op.create_index("ix_project_shares_project_id", "project_shares", ["project_id"])

    op.create_table(
        "shared_tasks_received",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_ccm_url", sa.String(500), nullable=False),
        sa.Column("owner_name", sa.String(100), nullable=True),
        sa.Column("owner_feishu_open_id", sa.String(100), nullable=True),
        sa.Column("remote_task_id", sa.Integer(), nullable=False),
        sa.Column("share_token", sa.String(200), nullable=False),
        sa.Column("task_title", sa.String(200), nullable=True),
        sa.Column("task_description", sa.Text(), nullable=True),
        sa.Column("project_name", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("received_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("owner_ccm_url", "remote_task_id", name="uq_shared_received_owner_task"),
    )


def downgrade() -> None:
    op.drop_table("shared_tasks_received")
    op.drop_table("project_shares")
    op.drop_table("task_shares")
