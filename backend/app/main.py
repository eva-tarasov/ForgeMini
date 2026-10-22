from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.workspaces import router as workspaces_router
from app.services.workspaces import WorkspaceService


def create_app() -> FastAPI:
    app = FastAPI(title="Forge API")
    app.state.workspace_service = WorkspaceService()
    app.include_router(health_router)
    app.include_router(workspaces_router)
    return app


app = create_app()
