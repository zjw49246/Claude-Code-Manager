"""add_enabled_skills_and_monitor_tables

Revision ID: a60bd886e7d9
Revises: 0aa352edf132
Create Date: 2026-06-09 10:36:03.946992

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a60bd886e7d9'
down_revision: Union[str, None] = '0aa352edf132'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('monitor_sessions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('monitor_context', sa.Text(), nullable=True),
    sa.Column('interval', sa.Integer(), nullable=False),
    sa.Column('max_checks', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('checks_done', sa.Integer(), nullable=False),
    sa.Column('last_summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('monitor_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_monitor_sessions_task_id'), ['task_id'], unique=False)

    op.create_table('monitor_checks',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('monitor_session_id', sa.Integer(), nullable=False),
    sa.Column('check_number', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('full_output', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('monitor_checks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_monitor_checks_monitor_session_id'), ['monitor_session_id'], unique=False)

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('enabled_skills', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('enabled_skills')

    with op.batch_alter_table('monitor_checks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitor_checks_monitor_session_id'))
    op.drop_table('monitor_checks')

    with op.batch_alter_table('monitor_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitor_sessions_task_id'))
    op.drop_table('monitor_sessions')
