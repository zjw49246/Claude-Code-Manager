"""add Worker EC2 provision request journal

Revision ID: a4c8e2f19d77
Revises: 31fe767354b7
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c8e2f19d77"
down_revision: Union[str, None] = "31fe767354b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provision_spec", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers", schema=None) as batch_op:
        batch_op.drop_column("provision_spec")
