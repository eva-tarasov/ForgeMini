from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceModel(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("name ~ '[^[:space:]]'", name="ck_workspaces_name_not_blank"),
        CheckConstraint(
            "(source_path IS NULL OR source_path ~ '[^[:space:]]') "
            "AND (test_command IS NULL OR test_command ~ '[^[:space:]]') "
            "AND (image IS NULL OR image ~ '[^[:space:]]')",
            name="ck_workspaces_run_config_not_blank",
        ),
        CheckConstraint("setup_command IS NULL OR setup_command ~ '[^[:space:]]'",
                        name="ck_workspaces_setup_command_not_blank"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    setup_command: Mapped[str | None] = mapped_column(Text, nullable=True)
