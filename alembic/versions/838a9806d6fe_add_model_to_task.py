"""add model to task

Revision ID: 838a9806d6fe
Revises: f3a8b2c1d9e0
Create Date: 2026-03-26 17:14:56.560516

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '838a9806d6fe'
down_revision: Union[str, None] = 'f3a8b2c1d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('model')
