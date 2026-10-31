from fastapi import FastAPI
from app.api.error_handlers import register_exception_handlers
from app.api.health import router as health_router
from app.api.tasks import router as tasks_router
from app.api.workspaces import router as workspaces_router
from app.services.tasks import TaskService
from app.services.workspaces import WorkspaceService


def create_app() -> FastAPI:
    app = FastAPI(title="Forge API")
    register_exception_handlers(app)
    workspace_service = WorkspaceService()
    app.state.workspace_service = workspace_service
    app.state.task_service = TaskService(workspace_service)
    app.include_router(health_router)
    app.include_router(workspaces_router)
    app.include_router(tasks_router)
    return app


app = create_app()
