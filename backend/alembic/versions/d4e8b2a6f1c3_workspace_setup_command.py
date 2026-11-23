"""Workspace setup command: the opt-in setup phase with network before the sandbox (ADR-004).

Nullable: existing workspaces simply have no setup phase.

Revision ID: d4e8b2a6f1c3
Revises: c5a1f0d2e7b9
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e8b2a6f1c3"
down_revision = "c5a1f0d2e7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("setup_command", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_setup_command_not_blank", "workspaces",
        "setup_command IS NULL OR setup_command ~ '[^[:space:]]'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_setup_command_not_blank", "workspaces", type_="check")
    op.drop_column("workspaces", "setup_command")
