"""add max_tasks to workers and worker_id to projects

Revision ID: d010371017ae
Revises: c3b6b12bda28
Create Date: 2026-07-02

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd010371017ae'
down_revision: Union[str, None] = 'c3b6b12bda28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_tasks', sa.Integer(), server_default='8', nullable=False))

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('worker_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_projects_worker_id'), ['worker_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_worker_id'))
        batch_op.drop_column('worker_id')

    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.drop_column('max_tasks')
