"""add last_accessed_at + sort_order to tasks

Revision ID: b8e2f3a4c5d6
Revises: a7f1c2d3e4b5
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e2f3a4c5d6'
down_revision: Union[str, None] = 'a7f1c2d3e4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('last_accessed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('sort_order', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('sort_order')
        batch_op.drop_column('last_accessed_at')
