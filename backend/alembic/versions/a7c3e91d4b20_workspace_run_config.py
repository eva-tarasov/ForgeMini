"""Workspace run configuration: where the code is, how to test it, which image to run in.

Columns are nullable: workspaces created before ForgeMini have no configuration and
simply cannot be used for sandbox runs (the run fails with a clear reason).

Revision ID: a7c3e91d4b20
Revises: 1d972cc34fbe
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c3e91d4b20"
down_revision = "1d972cc34fbe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("source_path", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("test_command", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("image", sa.String(200), nullable=True))
    op.create_check_constraint(
        "ck_workspaces_run_config_not_blank", "workspaces",
        "(source_path IS NULL OR source_path ~ '[^[:space:]]') "
        "AND (test_command IS NULL OR test_command ~ '[^[:space:]]') "
        "AND (image IS NULL OR image ~ '[^[:space:]]')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_run_config_not_blank", "workspaces", type_="check")
    op.drop_column("workspaces", "image")
    op.drop_column("workspaces", "test_command")
    op.drop_column("workspaces", "source_path")
