"""add facilitator_session_id to discussions

Revision ID: 72175997f1a6
Revises: 22c81d6f41b6
Create Date: 2026-06-04 13:03:22.765112

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '72175997f1a6'
down_revision: Union[str, None] = '22c81d6f41b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('discussions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('facilitator_session_id', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('discussions', schema=None) as batch_op:
        batch_op.drop_column('facilitator_session_id')
