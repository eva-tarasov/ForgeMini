import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import partial
from hashlib import sha256
from uuid import UUID, uuid4

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.permissions.policy import PermissionOutcome
from app.services.tool_calls import ToolCallTicket
from app.tools.contracts import ToolRiskLevel
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import RegisteredTool
from app.tools.repository.handlers import apply_patch
from app.tools.repository.patch import PatchTextError, parse_patch
from app.tools.repository.schemas import ApplyPatchInput, ApplyPatchOutput
from app.tools.repository.writer import PatchApplyError, SafeRepositoryWriter, SourceFingerprint


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalReference:
    """Metadata to locate a live request. Possession of it conveys no authority."""

    approval_id: UUID
    ticket: ToolCallTicket
    patch_sha256: str
    sources_sha256: str

    def __post_init__(self) -> None:
        if type(self.ticket) is not ToolCallTicket or any(
            type(identifier) is not UUID
            for identifier in (self.approval_id, self.ticket.run_id, self.ticket.workspace_id, self.ticket.tool_call_id)
        ):
            raise ValueError("invalid_approval_reference")
        if type(self.ticket.tool_version) is not int or self.ticket.tool_version < 1:
            raise ValueError("invalid_approval_reference")
        if type(self.ticket.tool_name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.ticket.tool_name):
            raise ValueError("invalid_approval_reference")
        if any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (self.patch_sha256, self.sources_sha256)):
            raise ValueError("invalid_approval_reference")

    def requested_payload(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "approval_id": str(self.approval_id),
            "tool_call_id": str(self.ticket.tool_call_id),
            "workspace_id": str(self.ticket.workspace_id),
            "tool_name": self.ticket.tool_name,
            "tool_version": self.ticket.tool_version,
            "patch_sha256": self.patch_sha256,
            "sources_sha256": self.sources_sha256,
        }


def patch_fingerprint(text: str) -> str:
    return sha256(b"forge:apply_patch:v1\0" + text.encode("utf-8", errors="strict")).hexdigest()


def sources_fingerprint(sources: tuple[SourceFingerprint, ...]) -> str:
    encoded = json.dumps([asdict(source) for source in sources], sort_keys=True, separators=(",", ":"))
    return sha256(b"forge:apply_patch:sources:v1\0" + encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingOperation:
    reference: ApprovalReference
    context: PermissionContext = field(repr=False)
    tool: RegisteredTool = field(repr=False)
    writer: SafeRepositoryWriter = field(repr=False)
    patch: str = field(repr=False)
    sources: tuple[SourceFingerprint, ...] = field(repr=False)
    deadline: float

    @classmethod
    def create(
        cls, context: PermissionContext, tool: RegisteredTool, writer: SafeRepositoryWriter,
        arguments: ApplyPatchInput, sources: tuple[SourceFingerprint, ...], deadline: float,
    ) -> "PendingOperation":
        ticket = ToolCallTicket(uuid4(), context.run_id, context.workspace_id, tool.spec.name, tool.spec.version)
        return cls(
            ApprovalReference(uuid4(), ticket, patch_fingerprint(arguments.patch), sources_fingerprint(sources)),
            context, tool, writer, arguments.patch, sources, deadline,
        )

    def check_identity(self) -> None:
        if type(self.reference) is not ApprovalReference or type(self.tool) is not RegisteredTool:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        ticket = self.reference.ticket
        try:
            paths = parse_patch(self.patch).paths
        except PatchTextError:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID) from None
        if (
            type(self.context) is not PermissionContext or self.context is not self.tool.bound_context
            or (ticket.run_id, ticket.workspace_id) != (self.context.run_id, self.context.workspace_id)
            or (ticket.tool_name, ticket.tool_version) != (self.tool.spec.name, self.tool.spec.version)
            or not math.isfinite(self.deadline)
            or type(self.sources) is not tuple or any(type(source) is not SourceFingerprint for source in self.sources)
            or tuple(source.path for source in self.sources) != paths
            or patch_fingerprint(self.patch) != self.reference.patch_sha256
            or sources_fingerprint(self.sources) != self.reference.sources_sha256
        ):
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)


def validate_patch_proposal(
    executor: ToolExecutor, name: str, arguments: dict[str, object], *,
    version: int | None, context: PermissionContext | None,
) -> tuple[RegisteredTool, ApplyPatchInput, SafeRepositoryWriter]:
    tool, typed, outcome, paths = executor._prepare_call(name, arguments, version=version, context=context)
    spec, handler = tool.spec, tool.handler
    if (
        spec.name != "apply_patch" or spec.version != 1 or spec.capability is not Capability.REPOSITORY_WRITE
        or spec.risk_level is not ToolRiskLevel.HIGH or spec.side_effect is not True
        or spec.requires_approval is not True or outcome is not PermissionOutcome.APPROVAL_REQUIRED
        or spec.input_schema is not ApplyPatchInput or spec.output_schema is not ApplyPatchOutput
        or type(typed) is not ApplyPatchInput or tool.synthetic or tool.bound_context is not context
        or type(handler) is not partial or handler.func is not apply_patch or handler.args
        or handler.keywords.keys() != {"writer"}
    ):
        raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
    writer = handler.keywords["writer"]
    if (
        type(writer) is not SafeRepositoryWriter or writer._context is not context
        or writer._write_spec != spec or paths != parse_patch(typed.patch).paths
    ):
        raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
    executor._validate_handler(tool)
    try:
        writer._authorize(paths)
    except PatchApplyError:
        raise ToolExecutionError(ToolErrorCode.PERMISSION_DENIED) from None
    return tool, typed, writer
