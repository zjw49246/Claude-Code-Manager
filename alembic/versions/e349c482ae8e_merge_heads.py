"""merge heads

Revision ID: e349c482ae8e
Revises: 9a20991114b3, d010371017ae
Create Date: 2026-07-13 09:03:44.546345

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e349c482ae8e'
down_revision: Union[str, None] = ('9a20991114b3', 'd010371017ae')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
