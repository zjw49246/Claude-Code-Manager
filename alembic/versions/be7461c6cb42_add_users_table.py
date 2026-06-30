"""add users table

Revision ID: be7461c6cb42
Revises: a2628601782f
Create Date: 2026-06-30 15:10:30.075191

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'be7461c6cb42'
down_revision: Union[str, None] = 'a2628601782f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('avatar_url', sa.String(length=500), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('feishu_open_id', sa.String(length=100), nullable=False),
    sa.Column('feishu_name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_email'), ['email'], unique=True)
        batch_op.create_index(batch_op.f('ix_users_feishu_open_id'), ['feishu_open_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_feishu_open_id'))
        batch_op.drop_index(batch_op.f('ix_users_email'))

    op.drop_table('users')
