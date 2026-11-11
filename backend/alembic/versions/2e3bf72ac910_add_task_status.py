"""Add persisted task status.

Revision ID: 2e3bf72ac910
Revises: ecb758eac83b
"""
from alembic import op
import sqlalchemy as sa

revision = "2e3bf72ac910"
down_revision = "ecb758eac83b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
    )
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('queued', 'running', 'waiting_approval', 'verifying', 'succeeded', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.drop_column("tasks", "status")
