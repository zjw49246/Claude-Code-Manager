"""add goal mode fields to task

Revision ID: 70c7c8140b1a
Revises: 357a4a51a397
Create Date: 2026-05-12 12:21:56.080727

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70c7c8140b1a'
down_revision: Union[str, None] = '357a4a51a397'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('goal_condition', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('goal_evaluator_model', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('goal_max_turns', sa.Integer(), server_default='30', nullable=False))
        batch_op.add_column(sa.Column('goal_turns_used', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('goal_last_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('goal_last_reason')
        batch_op.drop_column('goal_turns_used')
        batch_op.drop_column('goal_max_turns')
        batch_op.drop_column('goal_evaluator_model')
        batch_op.drop_column('goal_condition')
