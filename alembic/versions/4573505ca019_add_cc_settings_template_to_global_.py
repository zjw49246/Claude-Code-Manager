"""add cc_settings_template to global_settings

Revision ID: 4573505ca019
Revises: e349c482ae8e
Create Date: 2026-07-13 09:03:52.448950

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4573505ca019'
down_revision: Union[str, None] = 'e349c482ae8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cc_settings_template', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('global_settings', schema=None) as batch_op:
        batch_op.drop_column('cc_settings_template')
