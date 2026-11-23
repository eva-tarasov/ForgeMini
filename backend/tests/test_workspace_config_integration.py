"""Opt-in PostgreSQL check of the workspace run-config columns (migrations a7c3e91d4b20, d4e8b2a6f1c3)."""

import asyncio
import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.core.settings import Settings
from app.db.models import WorkspaceModel
from app.db.session import create_engine, create_session_factory
from app.domain.workspace import Workspace
from app.repositories.workspaces import WorkspaceRepository

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None, reason="set FORGE_TASK06_TEST_DB and POSTGRES_DB to opt in",
)


def _settings() -> Settings:
    name = os.getenv("FORGE_TASK06_TEST_DB")
    if not name or not re.fullmatch(r"forge_task06_[a-z0-9_]+", name) or os.getenv("POSTGRES_DB") != name:
        pytest.fail("POSTGRES_DB and FORGE_TASK06_TEST_DB must name the same forge_task06_* database", pytrace=False)
    return Settings(postgres_db=name)


def test_run_config_roundtrip_and_legacy_rows_and_blank_rejection():
    async def body():
        engine = create_engine(_settings())
        factory = create_session_factory(engine)
        full, legacy = uuid4(), uuid4()
        try:
            async with factory() as session:
                repo = WorkspaceRepository(session)
                await repo.add(Workspace(full, "cfg", "/src/app", "pytest -q", "python:3.12-slim",
                                         "pip install -r requirements.txt"))
                await repo.add(Workspace(legacy, "legacy"))          # no config: must still be valid
                await session.commit()
            async with factory() as session:
                repo = WorkspaceRepository(session)
                assert await repo.get(full) == Workspace(full, "cfg", "/src/app", "pytest -q", "python:3.12-slim",
                                                         "pip install -r requirements.txt")
                assert await repo.get(legacy) == Workspace(legacy, "legacy", None, None, None, None)
            # the DB itself (not only the API schema) refuses whitespace-only config
            async with factory() as session:
                session.add(WorkspaceModel(id=uuid4(), name="bad", test_command="   "))
                with pytest.raises(IntegrityError):
                    await session.commit()
            async with factory() as session:
                session.add(WorkspaceModel(id=uuid4(), name="bad", setup_command=" \t "))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            async with factory.begin() as session:
                await session.execute(delete(WorkspaceModel).where(WorkspaceModel.id.in_([full, legacy])))
            await engine.dispose()
    asyncio.run(asyncio.wait_for(body(), 60))
