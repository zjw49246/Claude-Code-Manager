"""add cli provider to tasks and instances

Revision ID: 9d1c3e4f5a6b
Revises: 70c7c8140b1a
Create Date: 2026-06-04 20:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9d1c3e4f5a6b"
down_revision: Union[str, None] = "70c7c8140b1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("instances", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider", sa.String(length=20), server_default="claude", nullable=False)
        )

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider", sa.String(length=20), server_default="claude", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("provider")

    with op.batch_alter_table("instances", schema=None) as batch_op:
        batch_op.drop_column("provider")
