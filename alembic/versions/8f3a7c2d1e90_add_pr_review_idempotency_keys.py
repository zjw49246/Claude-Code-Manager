"""add PR review idempotency keys

Revision ID: 8f3a7c2d1e90
Revises: 31fe767354b7
Create Date: 2026-07-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8f3a7c2d1e90"
down_revision: Union[str, None] = "31fe767354b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows retain NULL keys and therefore do not conflict. All new
    # GitHub webhook reviews populate both fields before they are inserted.
    with op.batch_alter_table("pr_reviews", schema=None) as batch_op:
        batch_op.add_column(sa.Column("head_sha", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("delivery_id", sa.String(length=100), nullable=True))
        batch_op.create_unique_constraint(
            "uq_pr_reviews_repo_pr_head",
            ["repo_id", "pr_number", "head_sha"],
        )
        batch_op.create_unique_constraint(
            "uq_pr_reviews_repo_delivery",
            ["repo_id", "delivery_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("pr_reviews", schema=None) as batch_op:
        batch_op.drop_constraint("uq_pr_reviews_repo_delivery", type_="unique")
        batch_op.drop_constraint("uq_pr_reviews_repo_pr_head", type_="unique")
        batch_op.drop_column("delivery_id")
        batch_op.drop_column("head_sha")
