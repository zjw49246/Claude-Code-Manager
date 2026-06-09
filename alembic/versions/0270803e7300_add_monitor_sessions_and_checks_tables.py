"""add_monitor_sessions_and_checks_tables

Revision ID: 0270803e7300
Revises: 0aa352edf132
Create Date: 2026-06-09 05:20:56.694830

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0270803e7300'
down_revision: Union[str, None] = '0aa352edf132'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('monitor_sessions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('monitor_context', sa.Text(), nullable=True),
    sa.Column('interval', sa.Integer(), nullable=False),
    sa.Column('max_checks', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('checks_done', sa.Integer(), nullable=False),
    sa.Column('last_summary', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('monitor_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_monitor_sessions_status'), ['status'], unique=False)
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


def downgrade() -> None:
    with op.batch_alter_table('monitor_checks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitor_checks_monitor_session_id'))
    op.drop_table('monitor_checks')

    with op.batch_alter_table('monitor_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_monitor_sessions_task_id'))
        batch_op.drop_index(batch_op.f('ix_monitor_sessions_status'))
    op.drop_table('monitor_sessions')
