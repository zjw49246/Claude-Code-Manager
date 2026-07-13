"""add user_groups tables

Revision ID: cbd5401c2f79
Revises: 7f8511ac33d7
Create Date: 2026-07-01 09:29:21.228887

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'cbd5401c2f79'
down_revision: Union[str, None] = '7f8511ac33d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('user_groups',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('user_group_members',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('user_group_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_group_members_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_group_members_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('user_group_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_group_members_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_group_members_group_id'))
    op.drop_table('user_group_members')
    op.drop_table('user_groups')
