"""add shared task relay fields

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-22 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("shared_from_id", sa.Integer(), nullable=True))
    op.create_index("ix_tasks_shared_from_id", "tasks", ["shared_from_id"])
    op.add_column("shared_tasks_received", sa.Column("local_task_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("shared_tasks_received", "local_task_id")
    op.drop_index("ix_tasks_shared_from_id", "tasks")
    op.drop_column("tasks", "shared_from_id")
