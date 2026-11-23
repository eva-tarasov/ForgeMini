"""Composition root of ForgeMini: the one place that wires every piece into a runnable server.

M1's `app = create_app()` had no execution binding, so `POST /agent-runs/{id}/start` always answered 503
and nothing could run outside test harnesses. Run this instead:

    uvicorn app.bootstrap:create_forge_app --factory --host 127.0.0.1 --port 8000

Settings come from the environment / project-root .env (the OpenAI key lives ONLY there).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.sandbox_runs import router as sandbox_runs_router
from app.core.settings import ROOT_ENV, Settings
from app.domain.execution import AgentRunStatus
from app.main import create_app
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.sandbox.agent_loop import LoopBudget
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.docker_sandbox import SandboxLimits, default_image
from app.sandbox.events import make_emit_factory
from app.sandbox.runner import SandboxJob, SandboxRunner, ToolCallingModel
from app.services.agent_cost import AgentPricing, ModelTariff
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.in_process_run_coordinator import RunExecutionBinding, RunnerConfigurationError
from app.services.sandbox_verifier import SandboxVerifier
from app.services.task_sync import sync_task_to_run

logger = logging.getLogger("forge.bootstrap")
SessionFactory = Callable[[], AsyncSession]
ModelFactory = Callable[[], ToolCallingModel]


class ForgeMiniSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, env_file_encoding="utf-8", extra="ignore",
                                      hide_input_in_errors=True)
    openai_api_key: SecretStr | None = None
    forge_model_name: str | None = None
    forge_model_timeout_seconds: float = Field(default=120.0, gt=0)     # one LLM call (M1 default was 15 s)
    forge_run_timeout_seconds: float = Field(default=900.0, gt=0)       # whole agent run (M1 was hard-wired to 60 s)
    forge_verify_timeout_seconds: float = Field(default=300.0, gt=0)    # one test-command run in the verifier
    forge_agent_max_steps: int = Field(default=25, ge=1, le=200)
    forge_sandbox_memory: str = "1g"
    forge_sandbox_cpus: float = Field(default=1.0, gt=0)
    forge_sandbox_image: str | None = None                              # default image for workspaces without one
    forge_data_dir: Path = Path("./.forgemini")                         # artifacts: patches, transcripts
    # Tariff of FORGE_MODEL_NAME in USD per 1M tokens, copied by the owner from the provider's price page. Not
    # hard-coded: prices change and differ per model. Without input+output prices usage is recorded, cost is "unknown".
    forge_price_input_usd_per_million: Decimal | None = None
    forge_price_output_usd_per_million: Decimal | None = None
    forge_price_cached_input_usd_per_million: Decimal | None = None     # optional; see agent_cost (cache split)
    forge_price_version: str = "env"                                    # free label, e.g. the date you copied it


def model_pricing(cfg: ForgeMiniSettings) -> AgentPricing:
    """One tariff for every call of the sandbox loop. Invalid values fail at startup, not in the middle of a run."""
    if cfg.forge_price_input_usd_per_million is None or cfg.forge_price_output_usd_per_million is None:
        return AgentPricing()
    if not cfg.forge_model_name:
        raise ValueError("FORGE_PRICE_* is set but FORGE_MODEL_NAME is not: the tariff must name its model")
    tariff = ModelTariff(
        provider="openai", model=cfg.forge_model_name, tariff_version=cfg.forge_price_version,
        input_usd_per_million=cfg.forge_price_input_usd_per_million,
        output_usd_per_million=cfg.forge_price_output_usd_per_million,
        cached_input_usd_per_million=cfg.forge_price_cached_input_usd_per_million,
    )
    return AgentPricing(research=tariff, finalization=tariff)


def openai_model_factory(cfg: ForgeMiniSettings) -> ModelFactory:
    def build() -> ToolCallingModel:
        if cfg.openai_api_key is None or not cfg.forge_model_name:
            raise RunnerConfigurationError(
                "OPENAI_API_KEY and FORGE_MODEL_NAME must be set in the server environment (.env)")
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=cfg.forge_model_name, api_key=cfg.openai_api_key,
                          timeout=cfg.forge_model_timeout_seconds, max_retries=2)   # type: ignore[return-value]
    return build


def create_forge_app(
    forge: ForgeMiniSettings | None = None, *, db_settings: Settings | None = None,
    session_factory: SessionFactory | None = None, model_factory: ModelFactory | None = None,
) -> FastAPI:
    cfg = forge or ForgeMiniSettings()
    if cfg.forge_sandbox_image:
        # default_image() reads os.environ only; this makes a value from .env effective too.
        os.environ["FORGE_SANDBOX_IMAGE"] = cfg.forge_sandbox_image
    artifacts = FileArtifactStore(cfg.forge_data_dir / "runs")
    make_model = model_factory or openai_model_factory(cfg)
    run_limits = SandboxLimits(memory=cfg.forge_sandbox_memory, cpus=cfg.forge_sandbox_cpus)
    verify_limits = SandboxLimits(memory=cfg.forge_sandbox_memory, cpus=cfg.forge_sandbox_cpus,
                                  command_timeout=cfg.forge_verify_timeout_seconds)

    def build_runner(factory: SessionFactory, run_id: UUID) -> SandboxRunner:
        model = make_model()                                    # raises RunnerConfigurationError -> clear 503

        async def resolve_job(request: AgentRunRequest) -> SandboxJob:
            async with factory() as session:
                task = await TaskRepository(session).get(request.task_id)
                ws = await WorkspaceRepository(session).get(task.workspace_id) if task else None
            if ws is None or not ws.source_path or not ws.test_command:
                raise AgentRunnerError("workspace_not_configured")
            return SandboxJob(Path(ws.source_path), ws.image or default_image(), ws.test_command, ws.setup_command)

        return SandboxRunner(
            model=model, resolve_job=resolve_job, artifacts=artifacts, limits=run_limits,
            budget=LoopBudget(max_steps=cfg.forge_agent_max_steps), emit_factory=make_emit_factory(factory))

    async def finalize(factory: SessionFactory, run_id: UUID) -> None:
        """After every run: independent verification (if the agent claimed success), then Task sync."""
        async with factory() as session:
            run = await AgentRunRepository(session).get(run_id)
            status = run.status if run else None
        if status == AgentRunStatus.VERIFYING.value:
            try:
                await SandboxVerifier(factory, artifacts, limits=verify_limits).verify(run_id)
            except Exception:
                logger.exception("sandbox_verification_failed")
        async with factory() as session:
            try:
                run = await AgentRunRepository(session).get_for_update(run_id)
                if run is not None:
                    task = await TaskRepository(session).get_for_update(run.task_id)
                    if task is not None:
                        sync_task_to_run(task, AgentRunStatus(run.status))
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    binding = RunExecutionBinding(
        workspace_id=None, build_runner=build_runner,
        execution_timeout_seconds=cfg.forge_run_timeout_seconds, after_run=finalize, pricing=model_pricing(cfg))
    app = create_app(settings=db_settings, session_factory=session_factory, execution_binding=binding)
    app.state.artifact_store = artifacts
    app.include_router(sandbox_runs_router)
    return app
