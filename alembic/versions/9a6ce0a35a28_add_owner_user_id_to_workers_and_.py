"""add owner_user_id to workers and created_by to tasks

Revision ID: 9a6ce0a35a28
Revises: be7461c6cb42
Create Date: 2026-06-30 15:57:24.373650

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a6ce0a35a28'
down_revision: Union[str, None] = 'be7461c6cb42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_tasks_created_by'), ['created_by'], unique=False)

    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_user_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_workers_owner_user_id'), ['owner_user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('workers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workers_owner_user_id'))
        batch_op.drop_column('owner_user_id')

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tasks_created_by'))
        batch_op.drop_column('created_by')
