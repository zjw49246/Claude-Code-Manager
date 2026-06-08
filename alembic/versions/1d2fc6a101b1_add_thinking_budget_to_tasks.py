"""add thinking_budget to tasks

Revision ID: 1d2fc6a101b1
Revises: ad2f3b4c5d6e
Create Date: 2026-06-08 16:40:11.429395

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1d2fc6a101b1'
down_revision: Union[str, None] = 'ad2f3b4c5d6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('thinking_budget', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('thinking_budget')
