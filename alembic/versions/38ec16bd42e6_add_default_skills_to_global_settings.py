"""add default skills to global settings

Revision ID: 38ec16bd42e6
Revises: d010371017ae
Create Date: 2026-07-06 11:58:50.270601

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '38ec16bd42e6'
down_revision: Union[str, None] = 'd010371017ae'
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
