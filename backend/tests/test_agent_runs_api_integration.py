"""HTTP create/get against an existing, opt-in PostgreSQL database."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from langchain_openai import ChatOpenAI
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentEventModel, AgentRunModel, TaskModel
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_runs import AgentRunCreateConflictError, AgentRunService
from app.services.agent_service import AgentService
from test_task_idempotency_integration import database


# The fixture DB must be migrated to the CURRENT head (read from the migration scripts, so a new migration does not
# require editing this test; it used to hard-code the revision and broke on every migration).
ALEMBIC_HEAD = ScriptDirectory.from_config(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))).get_current_head()

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_TASK06_TEST_DB") is None, reason="set FORGE_TASK06_TEST_DB to opt in to AgentRun HTTP tests",
)


@pytest.fixture
def task_case(database):
    @asynccontextmanager
    async def seeded(*, status="queued"):
        assert os.environ.get("POSTGRES_HOST") == "127.0.0.1"
        assert os.environ["POSTGRES_DB"].startswith("forge_task06_week4_audit_")
        async with database() as (factory, (workspace, _), task_ids):
            task_id = uuid4()
            task_ids.add(task_id)
            async with factory.begin() as session:
                assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
                assert await session.scalar(text("SELECT version_num FROM alembic_version")) == ALEMBIC_HEAD
                session.add(TaskModel(id=task_id, workspace_id=workspace, goal="Run API fixture",
                                      acceptance_criteria=["Persist an initial run"], status=status))
            try:
                async with asyncio.timeout(15):
                    yield factory, task_id
            finally:
                async with factory.begin() as session:
                    run_ids = select(AgentRunModel.id).where(AgentRunModel.task_id == task_id)
                    await session.execute(delete(AgentEventModel).where(AgentEventModel.agent_run_id.in_(run_ids)))
                    await session.execute(delete(AgentRunModel).where(AgentRunModel.task_id == task_id))

    return seeded


@asynccontextmanager
async def _client(factory):
    app = create_app(session_factory=factory)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://forge-tests.invalid", trust_env=False,
    ) as client:
        yield client


async def _runs(factory, task_id):
    async with factory() as session:
        return (await session.scalars(select(AgentRunModel).where(AgentRunModel.task_id == task_id))).all()


async def _assert_no_side_effect_records(factory, task_id):
    runs = select(AgentRunModel.id).where(AgentRunModel.task_id == task_id)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentEventModel).where(
            AgentEventModel.agent_run_id.in_(runs)
        )) == 0


@pytest.fixture
def no_execution(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Creating an AgentRun must not compose execution")

    for component in (AgentService, ChatOpenAI):
        monkeypatch.setattr(component, "__init__", unexpected)


def test_postgres_create_is_committed_and_does_not_start_execution(task_case, no_execution):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            response = await client.post(f"/tasks/{task_id}/runs")
            assert response.status_code == 201
            created = response.json()
            fetched = await client.get(f"/agent-runs/{created['id']}")
            assert fetched.status_code == 200 and fetched.json() == created
            repeated = await client.post(f"/tasks/{task_id}/runs")
            assert repeated.status_code == 200 and repeated.json() == created
            [run] = await _runs(factory, task_id)
            assert (str(run.id), run.task_id, run.status, run.attempt_number) == (created["id"], task_id, "queued", 1)
            async with factory() as session:
                assert await session.scalar(select(AgentRunModel.final_result.is_(None)).where(
                    AgentRunModel.id == run.id
                )) is True
                task = await session.get(TaskModel, task_id)
                assert task.status == "queued"
            await _assert_no_side_effect_records(factory, task_id)
    asyncio.run(check())


@pytest.mark.parametrize("run_status", ["running", "cancelled"])
def test_postgres_create_conflicts_after_start_or_terminal_status(task_case, run_status):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            created = await client.post(f"/tasks/{task_id}/runs")
            assert created.status_code == 201
            [run] = await _runs(factory, task_id)
            async with factory() as session:
                service = AgentRunService(session)
                if run_status == "running":
                    await service.claim_for_execution(run.id)
                else:
                    await service.cancel_execution(run.id)
            denied = await client.post(f"/tasks/{task_id}/runs")
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "AGENT_RUN_CREATE_CONFLICT"
            assert len(await _runs(factory, task_id)) == 1
    asyncio.run(check())


def test_postgres_create_refuses_non_queued_task_without_runs(task_case, no_execution):
    async def check():
        async with task_case(status="running") as (factory, task_id), _client(factory) as client:
            refused = await client.post(f"/tasks/{task_id}/runs")
            assert refused.status_code == 409 and refused.json()["error"]["code"] == "AGENT_RUN_CREATE_CONFLICT"
            assert await _runs(factory, task_id) == []
    asyncio.run(check())


@pytest.mark.parametrize("task_status", ["queued", "running"], ids=["task-queued", "task-running"])
def test_postgres_replays_queued_initial_run_regardless_of_task_status(task_case, no_execution, task_status):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            created = await client.post(f"/tasks/{task_id}/runs")
            assert created.status_code == 201 and created.json()["attempt_number"] == 1
            if task_status != "queued":
                async with factory.begin() as session:
                    task = await session.get(TaskModel, task_id)
                    task.status = task_status
            replay = await client.post(f"/tasks/{task_id}/runs")
            assert replay.status_code == 200 and replay.json() == created.json()
            [run] = await _runs(factory, task_id)
            assert str(run.id) == created.json()["id"] and run.status == "queued"
            async with factory() as session:
                assert (await session.get(TaskModel, task_id)).status == task_status
            await _assert_no_side_effect_records(factory, task_id)
    asyncio.run(check())


@pytest.mark.parametrize("attempt_number", [2, 3], ids=["attempt-2", "attempt-3"])
def test_postgres_create_refuses_lone_queued_later_attempt(task_case, no_execution, attempt_number):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            run_id = uuid4()
            async with factory.begin() as session:
                session.add(AgentRunModel(id=run_id, task_id=task_id, attempt_number=attempt_number, status="queued"))

            response = await client.post(f"/tasks/{task_id}/runs")
            [run] = await _runs(factory, task_id)
            assert (run.id, run.attempt_number, run.status, run.final_result) == (run_id, attempt_number, "queued", None)
            async with factory() as session:
                assert (await session.get(TaskModel, task_id)).status == "queued"
            await _assert_no_side_effect_records(factory, task_id)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "AGENT_RUN_CREATE_CONFLICT"
    asyncio.run(check())


@pytest.mark.parametrize(
    "attempts", [((1, "queued"), (2, "queued")), ((1, "queued"), (2, "failed")), ((2, "queued"), (3, "queued"))],
    ids=["two-queued", "queued-initial-and-terminal", "without-initial"],
)
def test_postgres_create_refuses_multiple_runs(task_case, no_execution, attempts):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            existing = {uuid4(): (number, status) for number, status in attempts}
            async with factory.begin() as session:
                session.add_all([
                    AgentRunModel(id=run_id, task_id=task_id, attempt_number=number, status=status)
                    for run_id, (number, status) in existing.items()
                ])

            response = await client.post(f"/tasks/{task_id}/runs")
            assert response.status_code == 409 and response.json()["error"]["code"] == "AGENT_RUN_CREATE_CONFLICT"
            assert {run.id: (run.attempt_number, run.status) for run in await _runs(factory, task_id)} == existing
            await _assert_no_side_effect_records(factory, task_id)
    asyncio.run(check())


def test_postgres_metadata_get_excludes_saved_result(task_case):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            run_id = uuid4()
            async with factory.begin() as session:
                session.add(AgentRunModel(
                    id=run_id, task_id=task_id, attempt_number=1, status="verifying",
                    final_result={"synthetic_private_result": "/private/synthetic-root"},
                ))
            response = await client.get(f"/agent-runs/{run_id}")
            assert response.status_code == 200
            assert response.json()["status"] == "verifying"
            assert set(response.json()) == {"id", "task_id", "attempt_number", "status", "created_at"}
            assert "synthetic" not in response.text
    asyncio.run(check())


def test_postgres_create_uses_actual_locked_task_status_instead_of_cached_status(task_case):
    async def check():
        async with task_case() as (factory, task_id), factory() as old_session:
            cached_task = await old_session.get(TaskModel, task_id)
            assert cached_task.status == "queued"
            async with factory.begin() as writer:
                task = await writer.get(TaskModel, task_id)
                task.status = "running"
            with pytest.raises(AgentRunCreateConflictError):
                await AgentRunService(old_session).create_initial_run(task_id)
            assert not old_session.in_transaction()
            assert await _runs(factory, task_id) == []
    asyncio.run(check())


def test_postgres_concurrent_posts_wait_for_task_lock_and_return_one_run(task_case, monkeypatch):
    async def check():
        async with task_case() as (factory, task_id), _client(factory) as client:
            staged, release = asyncio.Event(), asyncio.Event()
            original_lock = TaskRepository.get_for_update
            original_add = AgentRunRepository.add_initial
            pids = []

            async def record_lock(repo, key):
                pids.append(await repo.session.scalar(text("SELECT pg_backend_pid()")))
                return await original_lock(repo, key)

            async def hold_after_insert(repo, key):
                record = await original_add(repo, key)
                assert record.id is not None
                staged.set()
                await release.wait()
                return record

            monkeypatch.setattr(TaskRepository, "get_for_update", record_lock)
            monkeypatch.setattr(AgentRunRepository, "add_initial", hold_after_insert)
            requests = [asyncio.create_task(client.post(f"/tasks/{task_id}/runs"))]
            try:
                await staged.wait()
                assert await _runs(factory, task_id) == []
                requests.append(asyncio.create_task(client.post(f"/tasks/{task_id}/runs")))
                async with factory() as observer:
                    while True:
                        if len(pids) == 2:
                            blockers = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[1]})
                            if pids[0] in blockers:
                                break
                        assert not requests[1].done()
                        await asyncio.sleep(0.01)
                release.set()
                first, second = await asyncio.gather(*requests)
                assert (first.status_code, second.status_code) == (201, 200)
                assert first.json() == second.json()
                [run] = await _runs(factory, task_id)
                assert str(run.id) == first.json()["id"]
            finally:
                release.set()
                for request in requests:
                    if not request.done():
                        request.cancel()
                await asyncio.gather(*requests, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["insert", "commit", "cancellation"])
def test_postgres_failed_create_rolls_back_before_response(task_case, monkeypatch, failure):
    async def check():
        async with task_case() as (factory, task_id):
            staged = asyncio.Event()
            errors = []

            class FailingSession(AsyncSession):
                async def commit(self):
                    await self.flush()
                    assert await _runs(factory, task_id) == []
                    if failure == "cancellation":
                        staged.set()
                        await asyncio.Event().wait()
                    raise RuntimeError("synthetic commit failure")

            if failure == "insert":
                original = AgentRunRepository.add_initial

                async def violate_unique_constraint(repo, key):
                    record = await original(repo, key)
                    repo.session.add(AgentRunModel(task_id=key, attempt_number=1, status="queued"))
                    try:
                        await repo.session.flush()
                    except IntegrityError as exc:
                        errors.append(exc.orig.sqlstate)
                        raise
                    return record

                with monkeypatch.context() as patch:
                    patch.setattr(AgentRunRepository, "add_initial", violate_unique_constraint)
                    async with _client(factory) as client:
                        response = await client.post(f"/tasks/{task_id}/runs")
                        assert response.status_code == 500 and "23505" not in response.text
                assert errors == ["23505"]
            else:
                failing_factory = async_sessionmaker(factory.kw["bind"], class_=FailingSession, expire_on_commit=True)
                async with _client(failing_factory) as client:
                    request = asyncio.create_task(client.post(f"/tasks/{task_id}/runs"))
                    try:
                        if failure == "cancellation":
                            await staged.wait()
                            request.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await request
                        else:
                            response = await request
                            assert response.status_code == 500 and "synthetic commit failure" not in response.text
                    finally:
                        if not request.done():
                            request.cancel()
                            await asyncio.gather(request, return_exceptions=True)
            assert await _runs(factory, task_id) == []
            async with _client(factory) as client:
                assert (await client.post(f"/tasks/{task_id}/runs")).status_code == 201
            await _assert_no_side_effect_records(factory, task_id)
    asyncio.run(check())
