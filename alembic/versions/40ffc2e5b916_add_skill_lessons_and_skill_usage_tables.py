"""add skill_lessons and skill_usage tables

Revision ID: 40ffc2e5b916
Revises: 8e82faae4cfa
Create Date: 2026-06-17 08:19:42.014771

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '40ffc2e5b916'
down_revision: Union[str, None] = '8e82faae4cfa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'skill_lessons',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('skill_name', sa.String(100), nullable=False, index=True),
        sa.Column('lesson', sa.Text(), nullable=False),
        sa.Column('source', sa.String(50), server_default='evolution'),
        sa.Column('tool_name', sa.String(100), nullable=True),
        sa.Column('worker_id', sa.Integer(), nullable=True),
        sa.Column('lesson_hash', sa.String(32), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_table(
        'skill_usage',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('skill_name', sa.String(100), nullable=False, index=True),
        sa.Column('trigger_type', sa.String(50), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column('project_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('skill_usage')
    op.drop_table('skill_lessons')
