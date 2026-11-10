import asyncio
import os

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.settings import Settings
from app.db.session import create_engine, create_session_factory


pytestmark = pytest.mark.skipif(
    os.environ.get("FORGE_TEST_POSTGRES") != "1",
    reason="set FORGE_TEST_POSTGRES=1 to run the PostgreSQL integration test",
)


def run_database_check(check):
    failure = None
    try:
        asyncio.run(check())
    except (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        original = getattr(exc, "orig", exc)
        details = [type(original).__name__]
        error_number = getattr(original, "errno", None)
        if isinstance(error_number, int):
            details.append(f"errno={error_number}")
        sqlstate = getattr(original, "sqlstate", None)
        if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isascii() and sqlstate.isalnum():
            details.append(f"SQLSTATE={sqlstate}")
        failure = ", ".join(details)
    if failure:
        # Avoid rendering exception messages and chained errors that may contain secrets.
        pytest.fail(f"PostgreSQL connection/query failed ({failure})", pytrace=False)


def test_async_engine_connects_to_postgresql():
    async def check_connection():
        settings = Settings()
        engine = create_engine(settings)
        try:
            async with engine.connect() as connection:
                result = await connection.scalar(text("SELECT 1"))
                assert result == 1
        finally:
            await engine.dispose()

    run_database_check(check_connection)


def test_async_session_connects_to_postgresql():
    async def check_connection():
        settings = Settings()
        engine = create_engine(settings)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                database = await session.scalar(text("SELECT current_database()"))
                assert database == settings.postgres_db
        finally:
            await engine.dispose()

    run_database_check(check_connection)
