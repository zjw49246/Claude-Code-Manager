"""add feishu_user_binding and org tables

Revision ID: a1b2c3d4e5f6
Revises: 40ffc2e5b916
Create Date: 2026-06-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '40ffc2e5b916'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'feishu_user_binding',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('feishu_open_id', sa.String(100), nullable=False, unique=True),
        sa.Column('feishu_name', sa.String(100), nullable=True),
        sa.Column('avatar_url', sa.String(500), nullable=True),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('bound_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        'org_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('feishu_open_id', sa.String(100), nullable=False, unique=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('ccm_url', sa.String(500), nullable=False),
        sa.Column('avatar_url', sa.String(500), nullable=True),
        sa.Column('registered_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'org_teams',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        'org_team_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('team_id', sa.Integer(), sa.ForeignKey('org_teams.id', ondelete='CASCADE'), nullable=False),
        sa.Column('feishu_open_id', sa.String(100), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('org_team_members')
    op.drop_table('org_teams')
    op.drop_table('org_members')
    op.drop_table('feishu_user_binding')
