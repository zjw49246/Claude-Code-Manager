"""monitored_repos add provider

Revision ID: 31fe767354b7
Revises: e2e0716noop
Create Date: 2026-07-19 04:52:25.532936

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '31fe767354b7'
down_revision: Union[str, None] = 'e2e0716noop'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('monitored_repos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provider', sa.String(length=20), server_default='claude', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('monitored_repos', schema=None) as batch_op:
        batch_op.drop_column('provider')
