"""add team sharing tables

Revision ID: 317766a58b95
Revises: 9a6ce0a35a28
Create Date: 2026-06-30 16:14:38.095949

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '317766a58b95'
down_revision: Union[str, None] = '9a6ce0a35a28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('team_project_shares',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=False),
    sa.Column('target_id', sa.Integer(), nullable=False),
    sa.Column('shared_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'target_type', 'target_id', name='uq_team_project_share')
    )
    with op.batch_alter_table('team_project_shares', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_team_project_shares_project_id'), ['project_id'], unique=False)

    op.create_table('team_task_shares',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=False),
    sa.Column('target_id', sa.Integer(), nullable=False),
    sa.Column('permission', sa.String(length=20), nullable=False),
    sa.Column('shared_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'target_type', 'target_id', name='uq_team_task_share')
    )
    with op.batch_alter_table('team_task_shares', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_team_task_shares_task_id'), ['task_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('team_task_shares', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_team_task_shares_task_id'))
    op.drop_table('team_task_shares')

    with op.batch_alter_table('team_project_shares', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_team_project_shares_project_id'))
    op.drop_table('team_project_shares')
