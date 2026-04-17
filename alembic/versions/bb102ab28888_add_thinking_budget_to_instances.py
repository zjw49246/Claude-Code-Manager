"""add thinking_budget to instances

Revision ID: bb102ab28888
Revises: bd57a15b7d4a
Create Date: 2026-04-17 02:58:20.827748

Adds Instance.thinking_budget — optional Extended Thinking max-tokens budget
forwarded to Claude Code subprocess via the MAX_THINKING_TOKENS env var.
NULL means use the CLI default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bb102ab28888'
down_revision: Union[str, None] = 'bd57a15b7d4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instances', sa.Column('thinking_budget', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.drop_column('thinking_budget')
