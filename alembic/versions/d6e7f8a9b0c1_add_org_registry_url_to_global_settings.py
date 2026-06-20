"""add org_registry_url to global_settings

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-06-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("global_settings", sa.Column("org_registry_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("global_settings", "org_registry_url")
