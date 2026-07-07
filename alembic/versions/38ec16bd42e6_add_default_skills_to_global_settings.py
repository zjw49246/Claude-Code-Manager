"""add default skills to global settings

Revision ID: 38ec16bd42e6
Revises: a2628601782f
Create Date: 2026-07-06 11:58:50.270601

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '38ec16bd42e6'
# NOTE: originally pointed at 'd010371017ae' (worker max_tasks/worker_id migration
# from an unmerged branch that never landed on main) — that dangling reference broke
# `alembic upgrade head` (KeyError). Re-pointed to the real head at the time,
# a2628601782f (add_selected_user_skills_to_tasks).
down_revision: Union[str, None] = 'a2628601782f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('default_enabled_plugins', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('default_enabled_user_skills', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('default_enabled_user_skills')
        batch_op.drop_column('default_enabled_plugins')
