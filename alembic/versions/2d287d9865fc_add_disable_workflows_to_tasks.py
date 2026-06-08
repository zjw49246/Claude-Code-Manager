"""add disable_workflows to tasks

Revision ID: 2d287d9865fc
Revises: 1d2fc6a101b1
Create Date: 2026-06-08 19:05:45.469716

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d287d9865fc'
down_revision: Union[str, None] = '1d2fc6a101b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('disable_workflows', sa.Boolean(), server_default='1', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('disable_workflows')
