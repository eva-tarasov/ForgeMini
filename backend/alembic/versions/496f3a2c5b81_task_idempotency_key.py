"""Add workspace-scoped Task idempotency key.

Revision ID: 496f3a2c5b81
Revises: 2e3bf72ac910
"""
from alembic import op
import sqlalchemy as sa

revision = "496f3a2c5b81"
down_revision = "2e3bf72ac910"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("idempotency_key", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_tasks_workspace_idempotency_key", "tasks", ["workspace_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_tasks_workspace_idempotency_key", "tasks", type_="unique")
    op.drop_column("tasks", "idempotency_key")
