"""merge codex provider and quick phrases heads

Revision ID: ad2f3b4c5d6e
Revises: 9d1c3e4f5a6b, f863609e3498
Create Date: 2026-06-05 17:35:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "ad2f3b4c5d6e"
down_revision: Union[str, tuple[str, str], None] = ("9d1c3e4f5a6b", "f863609e3498")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
