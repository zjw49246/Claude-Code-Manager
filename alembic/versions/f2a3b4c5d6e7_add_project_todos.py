"""add project todos

Revision ID: f2a3b4c5d6e7
Revises: 38ec16bd42e6
Create Date: 2026-06-22 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
# Chains after 38ec16bd42e6 (the current head on main once its own broken parent
# pointer is repaired) so the tree stays single-headed and `upgrade head` works.
down_revision = "38ec16bd42e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_todos",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        # Soft provenance link to the spawned task (no FK; SQLite FKs are off).
        sa.Column("created_task_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_project_todos_project_id", "project_todos", ["project_id"])
    op.create_index(
        "ix_project_todos_project_status_sort",
        "project_todos",
        ["project_id", "status", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_todos_project_status_sort", table_name="project_todos")
    op.drop_index("ix_project_todos_project_id", table_name="project_todos")
    op.drop_table("project_todos")
