from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.agent_runs import router as agent_runs_router
from app.api.approvals import router as approvals_router
from app.api.error_handlers import register_exception_handlers
from app.api.evidence import router as evidence_router
from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.api.workspaces import router as workspaces_router
from app.core.request_logging import install_request_logging
from app.core.settings import Settings
from app.db.session import create_engine, create_session_factory
from app.services.in_process_run_coordinator import InProcessRunCoordinator, RunExecutionBinding
from app.services.local_approval_access import LocalApprovalAccessBinding


def create_app(
    *, settings: Settings | None = None, session_factory: Callable[[], AsyncSession] | None = None,
    execution_binding: RunExecutionBinding | None = None,
    approval_access_binding: LocalApprovalAccessBinding | None = None,
) -> FastAPI:
    """Create an app; the caller owns any injected session factory's engine."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        factory = session_factory
        if factory is None:
            engine = create_engine(settings if settings is not None else Settings())
            factory = create_session_factory(engine)
        coordinator = InProcessRunCoordinator(factory, execution_binding) if execution_binding is not None else None
        app.state.session_factory = factory
        app.state.run_coordinator = coordinator
        try:
            yield
        finally:
            try:
                if coordinator is not None:
                    await coordinator.close()
            finally:
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(title="Forge API", lifespan=lifespan)
    app.state.approval_access_binding = approval_access_binding
    register_exception_handlers(app)
    install_request_logging(app)
    app.include_router(health_router)
    app.include_router(workspaces_router)
    app.include_router(tasks_router)
    app.include_router(agent_runs_router)
    app.include_router(approvals_router)
    app.include_router(evidence_router)
    return app


app = create_app()
