"""add workers table

Revision ID: c9d8e7f6a5b4
Revises: b8e2f3a4c5d6
Create Date: 2026-06-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d8e7f6a5b4'
down_revision: Union[str, None] = 'b8e2f3a4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='creating'),
        sa.Column('cloud_instance_id', sa.String(length=100), nullable=True),
        sa.Column('private_ip', sa.String(length=45), nullable=True),
        sa.Column('public_ip', sa.String(length=45), nullable=True),
        sa.Column('adopted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('ssh_user', sa.String(length=50), nullable=False, server_default='ubuntu'),
        sa.Column('ssh_key_path', sa.String(length=500), nullable=True),
        sa.Column('ccm_port', sa.Integer(), nullable=False, server_default='8000'),
        sa.Column('auth_token', sa.String(length=128), nullable=True),
        sa.Column('ccm_commit', sa.String(length=64), nullable=True),
        sa.Column('accounts', sa.JSON(), nullable=True),
        sa.Column('project_mapping', sa.JSON(), nullable=True),
        sa.Column('last_heartbeat', sa.DateTime(), nullable=True),
        sa.Column('bootstrap_step', sa.String(length=100), nullable=True),
        sa.Column('bootstrap_error', sa.Text(), nullable=True),
        sa.Column('bootstrap_log', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('workers')
