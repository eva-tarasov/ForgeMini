from dataclasses import dataclass
from enum import StrEnum

from app.domain.capabilities import Capability
from app.permissions.context import PermissionContext
from app.permissions.paths import has_protected_component, matches_scope, parse_logical_path
from app.tools.contracts import ToolSpec


class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class PermissionReason(StrEnum):
    MISSING_CONTEXT = "missing_context"
    INVALID_CONTEXT = "invalid_context"
    CAPABILITY_NOT_GRANTED = "capability_not_granted"
    ACCESS_NOT_DECLARED = "access_not_declared"
    INVALID_SYNTHETIC_ACCESS = "invalid_synthetic_access"
    INVALID_PATH_SELECTION = "invalid_path_selection"
    INVALID_PATH = "invalid_path"
    PROTECTED_PATH = "protected_path"
    PATH_OUT_OF_SCOPE = "path_out_of_scope"
    APPROVAL_REQUIRED = "approval_required"
    AUTHORIZED = "authorized"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    outcome: PermissionOutcome
    reason: PermissionReason


class PermissionPolicy:
    def evaluate(
        self,
        spec: ToolSpec,
        context: PermissionContext | None,
        *,
        paths: tuple[str, ...] | None = None,
        synthetic: bool = False,
    ) -> PermissionDecision:
        if context is None:
            return PermissionDecision(PermissionOutcome.DENY, PermissionReason.MISSING_CONTEXT)
        if type(context) is not PermissionContext:
            return PermissionDecision(PermissionOutcome.DENY, PermissionReason.INVALID_CONTEXT)

        capability = spec.capability
        if not isinstance(capability, Capability) or capability not in context.capabilities:
            return PermissionDecision(PermissionOutcome.DENY, PermissionReason.CAPABILITY_NOT_GRANTED)
        is_read = capability is Capability.REPOSITORY_READ

        if type(synthetic) is not bool or (
            synthetic and (paths is not None or not is_read or spec.side_effect)
        ):
            return PermissionDecision(PermissionOutcome.DENY, PermissionReason.INVALID_SYNTHETIC_ACCESS)
        if not synthetic:
            if paths is None:
                return PermissionDecision(PermissionOutcome.DENY, PermissionReason.ACCESS_NOT_DECLARED)
            if type(paths) is not tuple or not paths:
                return PermissionDecision(PermissionOutcome.DENY, PermissionReason.INVALID_PATH_SELECTION)

            scopes = context.read_scopes if is_read else context.write_scopes
            for path in paths:
                try:
                    components = parse_logical_path(path)
                except (TypeError, ValueError):
                    return PermissionDecision(PermissionOutcome.DENY, PermissionReason.INVALID_PATH)
                if has_protected_component(components):
                    return PermissionDecision(PermissionOutcome.DENY, PermissionReason.PROTECTED_PATH)
                if not any(
                    matches_scope(components, parse_logical_path(scope.path), recursive=scope.recursive)
                    for scope in scopes
                ):
                    return PermissionDecision(PermissionOutcome.DENY, PermissionReason.PATH_OUT_OF_SCOPE)

        if not is_read or spec.requires_approval or spec.side_effect:
            return PermissionDecision(PermissionOutcome.APPROVAL_REQUIRED, PermissionReason.APPROVAL_REQUIRED)
        return PermissionDecision(PermissionOutcome.ALLOW, PermissionReason.AUTHORIZED)
