"""add created_by to tags

Revision ID: 7f8511ac33d7
Revises: 7ab221874b1c
Create Date: 2026-07-01 06:32:17.415548

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7f8511ac33d7'
down_revision: Union[str, None] = '7ab221874b1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_tags_created_by'), ['created_by'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('tags', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tags_created_by'))
        batch_op.drop_column('created_by')
