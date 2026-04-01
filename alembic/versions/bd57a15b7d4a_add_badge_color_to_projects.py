"""add badge_color to projects

Revision ID: bd57a15b7d4a
Revises: 838a9806d6fe
Create Date: 2026-03-31 19:29:17.842001

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bd57a15b7d4a'
down_revision: Union[str, None] = '838a9806d6fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('badge_color', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('badge_color')
