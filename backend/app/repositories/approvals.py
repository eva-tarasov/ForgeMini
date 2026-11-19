from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import String, cast, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEventModel, AgentRunModel, ApprovalModel, TaskModel, ToolCallModel
from app.domain.execution import ApprovalStatus, ToolCallStatus


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add_pending(self, approval_id: UUID, tool_call_id: UUID) -> None:
        self.session.add(ApprovalModel(
            id=approval_id, tool_call_id=tool_call_id, status=ApprovalStatus.PENDING.value,
        ))

    async def read_pending_for_run(
        self, run_id: UUID, workspace_id: UUID, *, limit: int,
    ) -> Sequence[RowMapping]:
        pending = (
            select(ApprovalModel.id, ToolCallModel.tool_name, ApprovalModel.status, ApprovalModel.requested_at)
            .join(ToolCallModel, ApprovalModel.tool_call_id == ToolCallModel.id)
            .where(ToolCallModel.agent_run_id == AgentRunModel.id, ApprovalModel.status == ApprovalStatus.PENDING.value)
            .order_by(ApprovalModel.requested_at, ApprovalModel.id)
            .limit(limit)
            .correlate(AgentRunModel)
            .lateral()
        )
        # Preserve the scoped run when it has no pending rows.
        statement = (
            select(AgentRunModel.id.label("run_id"), pending.c.id, pending.c.tool_name,
                   pending.c.status, pending.c.requested_at)
            .join(TaskModel, TaskModel.id == AgentRunModel.task_id)
            .outerjoin(pending, true())
            .where(AgentRunModel.id == run_id, TaskModel.workspace_id == workspace_id)
            .order_by(pending.c.requested_at, pending.c.id)
        )
        return (await self.session.execute(statement)).mappings().all()

    async def read_for_run(self, approval_id: UUID, run_id: UUID, workspace_id: UUID) -> RowMapping | None:
        statement = self._scoped_approval(approval_id, run_id, workspace_id)
        return (await self.session.execute(statement)).mappings().one_or_none()

    @staticmethod
    def _scoped_approval(approval_id: UUID, run_id: UUID, workspace_id: UUID):
        return (
            select(
                ApprovalModel.id, AgentRunModel.id.label("run_id"), ToolCallModel.tool_name,
                ApprovalModel.status, ApprovalModel.requested_at, ApprovalModel.decided_at,
                TaskModel.workspace_id, ToolCallModel.id.label("tool_call_id"),
                ToolCallModel.status.label("tool_call_status"), AgentRunModel.status.label("run_status"),
            )
            .join(ToolCallModel, ToolCallModel.id == ApprovalModel.tool_call_id)
            .join(AgentRunModel, AgentRunModel.id == ToolCallModel.agent_run_id)
            .join(TaskModel, TaskModel.id == AgentRunModel.task_id)
            .where(ApprovalModel.id == approval_id, AgentRunModel.id == run_id, TaskModel.workspace_id == workspace_id)
        )

    async def read_for_review(
        self, approval_id: UUID, run_id: UUID, workspace_id: UUID, *, request_event_type: str,
    ) -> RowMapping | None:
        # At most two request events: a duplicate is enough to fail the exact review check.
        requests = (
            select(AgentEventModel.payload)
            .where(
                AgentEventModel.agent_run_id == AgentRunModel.id,
                AgentEventModel.event_type == request_event_type,
                AgentEventModel.payload["approval_id"].astext == cast(ApprovalModel.id, String),
            )
            .correlate(AgentRunModel, ApprovalModel)
            .limit(2)
            .subquery()
        )
        history = select(func.jsonb_agg(requests.c.payload, type_=JSONB)).scalar_subquery()
        statement = self._scoped_approval(approval_id, run_id, workspace_id).add_columns(history.label("request_events"))
        return (await self.session.execute(statement)).mappings().one_or_none()

    async def has_active(self, run_id: UUID) -> bool:
        statement = (
            select(ApprovalModel.id)
            .join(ToolCallModel, ApprovalModel.tool_call_id == ToolCallModel.id)
            .where(
                ToolCallModel.agent_run_id == run_id,
                ToolCallModel.status == ToolCallStatus.PROPOSED.value,
                ApprovalModel.status.in_((ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)),
            )
            .limit(1)
        )
        return await self.session.scalar(statement) is not None

    @staticmethod
    def _owned_approval(approval_id: UUID, tool_call_id: UUID):
        return select(ApprovalModel).where(
            ApprovalModel.id == approval_id, ApprovalModel.tool_call_id == tool_call_id,
        )

    async def get(self, approval_id: UUID, tool_call_id: UUID) -> ApprovalModel | None:
        return await self.session.scalar(self._owned_approval(approval_id, tool_call_id))

    async def get_for_update(self, approval_id: UUID, tool_call_id: UUID) -> ApprovalModel | None:
        statement = self._owned_approval(approval_id, tool_call_id).with_for_update()
        return await self.session.scalar(statement.execution_options(populate_existing=True))

    async def event_payloads(self, run_id: UUID, approval_id: UUID, event_type: str) -> list[dict]:
        statement = select(AgentEventModel.payload).where(
            AgentEventModel.agent_run_id == run_id,
            AgentEventModel.event_type == event_type,
            AgentEventModel.payload["approval_id"].astext == str(approval_id),
        ).limit(2)
        return list((await self.session.scalars(statement)).all())
