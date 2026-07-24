"""merge PR review idempotency and worker provisioning heads

Revision ID: c7e9b1d42f60
Revises: 8f3a7c2d1e90, a4c8e2f19d77
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "c7e9b1d42f60"
down_revision: Union[str, Sequence[str], None] = (
    "8f3a7c2d1e90",
    "a4c8e2f19d77",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
