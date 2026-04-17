"""add effort_level to instances and tasks

Revision ID: eeec980ad834
Revises: bb102ab28888
Create Date: 2026-04-17 08:58:34.360495

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eeec980ad834'
down_revision: Union[str, None] = 'bb102ab28888'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('effort_level', sa.String(length=20), nullable=True))

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('effort_level', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('effort_level')

    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.drop_column('effort_level')
