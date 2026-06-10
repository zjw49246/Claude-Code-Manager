"""add use_pty_mode to global_settings

Revision ID: b7c8d9e0f1a2
Revises: a60bd886e7d9
Create Date: 2026-06-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'a60bd886e7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('global_settings', sa.Column('use_pty_mode', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('global_settings', 'use_pty_mode')
