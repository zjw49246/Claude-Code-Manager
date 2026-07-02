"""add worker_id to monitored_repos

Revision ID: c3b6b12bda28
Revises: cbd5401c2f79
Create Date: 2026-07-02

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c3b6b12bda28'
down_revision: Union[str, None] = 'cbd5401c2f79'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('monitored_repos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('worker_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_monitored_repos_worker_id'), ['worker_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('monitored_repos', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitored_repos_worker_id'))
        batch_op.drop_column('worker_id')
