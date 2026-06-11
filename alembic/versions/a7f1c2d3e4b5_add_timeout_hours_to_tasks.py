"""add timeout_hours to tasks + backfill model/effort defaults

Revision ID: a7f1c2d3e4b5
Revises: 4eb9588e65fc
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7f1c2d3e4b5'
down_revision: Union[str, None] = '4eb9588e65fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('timeout_hours', sa.Float(), nullable=True))
    # 回填 pending 任务的 model/effort 默认值（设置归 Task，消除 instance fallback）
    op.execute(
        "UPDATE tasks SET model = 'claude-opus-4-6' WHERE model IS NULL AND status = 'pending'"
    )
    op.execute(
        "UPDATE tasks SET effort_level = 'medium' WHERE effort_level IS NULL AND status = 'pending'"
    )


def downgrade() -> None:
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('timeout_hours')
