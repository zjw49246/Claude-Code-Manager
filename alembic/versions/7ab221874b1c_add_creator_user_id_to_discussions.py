"""add creator_user_id to discussions

Revision ID: 7ab221874b1c
Revises: 317766a58b95
Create Date: 2026-07-01 02:54:38.043236

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7ab221874b1c'
down_revision: Union[str, None] = '317766a58b95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('discussions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('creator_user_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_discussions_creator_user_id'), ['creator_user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('discussions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_discussions_creator_user_id'))
        batch_op.drop_column('creator_user_id')
