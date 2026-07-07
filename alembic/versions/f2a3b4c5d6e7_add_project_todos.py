"""add project todos

Revision ID: f2a3b4c5d6e7
Revises: a2628601782f
Create Date: 2026-06-22 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
# NOTE: rebased onto the current head. The user_skills migrations
# (a70ee5479e2e → a2628601782f) landed after this branch was first cut, so this
# must chain off a2628601782f — otherwise two heads and `upgrade head` fails.
down_revision = "a2628601782f"
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
