"""task.worker_id + log_entries.instance_id nullable

Revision ID: d7e8f9a0b1c2
Revises: c9d8e7f6a5b4
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c9d8e7f6a5b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('worker_id', sa.Integer(), nullable=True))
    op.create_index('ix_tasks_worker_id', 'tasks', ['worker_id'])
    # 远程 task 的日志副本没有本地 instance → instance_id 放开 NOT NULL
    with op.batch_alter_table('log_entries') as batch_op:
        batch_op.alter_column('instance_id', existing_type=sa.Integer(), nullable=True)
    op.add_column('sub_agent_sessions', sa.Column('remote_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('sub_agent_sessions', 'remote_id')
    with op.batch_alter_table('log_entries') as batch_op:
        batch_op.alter_column('instance_id', existing_type=sa.Integer(), nullable=False)
    op.drop_index('ix_tasks_worker_id', table_name='tasks')
    op.drop_column('tasks', 'worker_id')
