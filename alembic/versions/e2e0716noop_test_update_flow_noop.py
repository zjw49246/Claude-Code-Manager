"""test update flow — no-op migration for E2E verification

Revision ID: e2e0716noop
Revises: 4573505ca019
Create Date: 2026-07-16 06:40:00.000000
"""
from typing import Sequence, Union

revision: str = "e2e0716noop"
down_revision: Union[str, None] = "4573505ca019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
