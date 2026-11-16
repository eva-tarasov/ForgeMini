from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.permissions.policy import PermissionDecision, PermissionOutcome, PermissionPolicy, PermissionReason
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec


class SampleInput(ToolInput):
    path: str


class SampleOutput(ToolOutput):
    accepted: bool


def make_context(**overrides: object) -> PermissionContext:
    fields = {
        "run_id": UUID(int=1),
        "workspace_id": UUID(int=2),
        "capabilities": frozenset({Capability.REPOSITORY_READ}),
    }
    return PermissionContext(**(fields | overrides))


def make_spec(capability: Capability, *, requires_approval: bool = False) -> ToolSpec:
    is_write = capability is Capability.REPOSITORY_WRITE
    return ToolSpec(
        name="sample_tool",
        version=1,
        description="Synthetic tool descriptor for permission tests.",
        input_schema=SampleInput,
        output_schema=SampleOutput,
        capability=capability,
        risk_level=ToolRiskLevel.HIGH if is_write else ToolRiskLevel.LOW,
        timeout_ms=1000,
        side_effect=is_write,
        requires_approval=is_write or requires_approval,
    )


READ_SPEC = make_spec(Capability.REPOSITORY_READ)
WRITE_SPEC = make_spec(Capability.REPOSITORY_WRITE)
ROOT_SCOPE = PathScope(".", recursive=True)
POLICY = PermissionPolicy()


def test_permission_context_preserves_immutable_explicit_grants() -> None:
    read_scope = PathScope("README.md")
    write_scope = PathScope("src", recursive=True)
    context = make_context(read_scopes=(read_scope,), write_scopes=(write_scope,))

    assert context.run_id == UUID(int=1)
    assert context.workspace_id == UUID(int=2)
    assert context.capabilities == frozenset({Capability.REPOSITORY_READ})
    assert isinstance(context.capabilities, frozenset)
    assert context.read_scopes == (read_scope,)
    assert context.write_scopes == (write_scope,)
    assert not hasattr(context, "__dict__")
    assert not hasattr(read_scope, "__dict__")
    with pytest.raises(FrozenInstanceError):
        context.read_scopes = ()
    with pytest.raises(FrozenInstanceError):
        read_scope.recursive = True


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": "not-a-uuid"},
        {"workspace_id": 1},
        {"capabilities": {Capability.REPOSITORY_READ}},
        {"capabilities": frozenset({"repository.read"})},
        {"read_scopes": [ROOT_SCOPE]},
        {"read_scopes": (".",)},
        {"write_scopes": [ROOT_SCOPE]},
        {"write_scopes": (None,)},
    ],
)
def test_permission_context_rejects_invalid_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        make_context(**overrides)


def test_path_scope_rejects_non_boolean_recursive_flag() -> None:
    with pytest.raises(TypeError, match="recursive flag must be a bool"):
        PathScope("src", recursive=1)


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (None, PermissionReason.MISSING_CONTEXT),
        ({}, PermissionReason.INVALID_CONTEXT),
        (make_context(capabilities=frozenset()), PermissionReason.CAPABILITY_NOT_GRANTED),
    ],
)
def test_policy_denies_missing_or_invalid_grants(context: object, reason: PermissionReason) -> None:
    assert POLICY.evaluate(READ_SPEC, context, synthetic=True) == PermissionDecision(
        PermissionOutcome.DENY, reason
    )


def test_policy_allows_explicit_synthetic_read() -> None:
    decision = POLICY.evaluate(READ_SPEC, make_context(), synthetic=True)

    assert decision == PermissionDecision(PermissionOutcome.ALLOW, PermissionReason.AUTHORIZED)
    assert decision.outcome.value == "allow"
    with pytest.raises(FrozenInstanceError):
        decision.outcome = PermissionOutcome.DENY


@pytest.mark.parametrize(
    ("granted", "spec"),
    [
        (Capability.REPOSITORY_READ, WRITE_SPEC),
        (Capability.REPOSITORY_WRITE, READ_SPEC),
    ],
    ids=["read-does-not-grant-write", "write-does-not-grant-read"],
)
def test_policy_requires_each_capability_explicitly(granted: Capability, spec: ToolSpec) -> None:
    context = make_context(
        capabilities=frozenset({granted}), read_scopes=(ROOT_SCOPE,), write_scopes=(ROOT_SCOPE,)
    )

    assert POLICY.evaluate(spec, context, paths=("src/main.py",)) == PermissionDecision(
        PermissionOutcome.DENY, PermissionReason.CAPABILITY_NOT_GRANTED
    )


@pytest.mark.parametrize(
    ("paths", "synthetic", "reason"),
    [
        (None, False, PermissionReason.ACCESS_NOT_DECLARED),
        ((), False, PermissionReason.INVALID_PATH_SELECTION),
        (["src/main.py"], False, PermissionReason.INVALID_PATH_SELECTION),
        ((), True, PermissionReason.INVALID_SYNTHETIC_ACCESS),
        (None, 1, PermissionReason.INVALID_SYNTHETIC_ACCESS),
    ],
)
def test_policy_denies_invalid_access_declaration(
    paths: object, synthetic: object, reason: PermissionReason
) -> None:
    context = make_context(read_scopes=(ROOT_SCOPE,))

    assert POLICY.evaluate(READ_SPEC, context, paths=paths, synthetic=synthetic) == PermissionDecision(
        PermissionOutcome.DENY, reason
    )


def test_policy_denies_synthetic_write() -> None:
    context = make_context(capabilities=frozenset({Capability.REPOSITORY_WRITE}))

    assert POLICY.evaluate(WRITE_SPEC, context, synthetic=True) == PermissionDecision(
        PermissionOutcome.DENY, PermissionReason.INVALID_SYNTHETIC_ACCESS
    )


def test_policy_denies_path_without_applicable_scopes() -> None:
    context = make_context(write_scopes=(ROOT_SCOPE,))

    assert POLICY.evaluate(READ_SPEC, context, paths=("src/main.py",)) == PermissionDecision(
        PermissionOutcome.DENY, PermissionReason.PATH_OUT_OF_SCOPE
    )


@pytest.mark.parametrize(
    ("scope", "path", "allowed"),
    [
        pytest.param(PathScope("README.md"), "README.md", True, id="exact-file"),
        pytest.param(PathScope("src"), "src/main.py", False, id="exact-scope-is-not-recursive"),
        pytest.param(PathScope("src", True), "src/main.py", True, id="recursive-child"),
        pytest.param(PathScope("src", True), "src", True, id="recursive-scope-itself"),
        pytest.param(PathScope("src", True), "src_backup/main.py", False, id="prefix-confusion"),
        pytest.param(PathScope("src", True), "Src/main.py", False, id="scope-case-sensitive"),
        pytest.param(PathScope("."), ".", True, id="exact-root"),
        pytest.param(PathScope("."), "README.md", False, id="exact-root-excludes-descendants"),
        pytest.param(ROOT_SCOPE, ".", True, id="recursive-root-itself"),
        pytest.param(ROOT_SCOPE, "src/main.py", True, id="recursive-root-descendant"),
    ],
)
def test_policy_matches_scope(scope: PathScope, path: str, allowed: bool) -> None:
    decision = POLICY.evaluate(READ_SPEC, make_context(read_scopes=(scope,)), paths=(path,))

    if allowed:
        assert decision == PermissionDecision(PermissionOutcome.ALLOW, PermissionReason.AUTHORIZED)
    else:
        assert decision == PermissionDecision(PermissionOutcome.DENY, PermissionReason.PATH_OUT_OF_SCOPE)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(None, id="non-string"),
        pytest.param("", id="empty"),
        pytest.param("/src/main.py", id="absolute-path"),
        pytest.param("src/../README.md", id="parent-traversal"),
        pytest.param("src/./main.py", id="dot-component"),
        pytest.param("src//main.py", id="repeated-separator"),
        pytest.param("src/", id="trailing-separator"),
        pytest.param("src\\main.py", id="backslash"),
        pytest.param("C:/src/main.py", id="drive-path"),
        pytest.param("//server/share/main.py", id="unc-path"),
        pytest.param("src/info:stream", id="colon"),
        pytest.param("src/\x00", id="nul"),
        pytest.param("src/\n", id="ascii-control"),
        pytest.param("src/\x7f", id="ascii-delete"),
        pytest.param("~/main.py", id="tilde"),
        pytest.param("x" * 4097, id="over-length-limit"),
    ],
)
def test_policy_denies_malformed_paths(path: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PathScope(path)

    assert POLICY.evaluate(READ_SPEC, make_context(read_scopes=(ROOT_SCOPE,)), paths=(path,)) == (
        PermissionDecision(PermissionOutcome.DENY, PermissionReason.INVALID_PATH)
    )


def test_policy_treats_encoded_and_environment_paths_as_literal() -> None:
    paths = ("src/%2e%2e/file", "$HOME/file", "x" * 4096)
    context = make_context(read_scopes=tuple(PathScope(path) for path in paths))

    assert POLICY.evaluate(READ_SPEC, context, paths=paths).outcome is PermissionOutcome.ALLOW


@pytest.mark.parametrize("path", [".git", "src/.GiT/config", ".env", "src/.ENV", "a/b/.EnV.Local"])
def test_policy_rejects_protected_components_even_when_explicitly_scoped(path: str) -> None:
    context = make_context(read_scopes=(ROOT_SCOPE, PathScope(path)))

    assert POLICY.evaluate(READ_SPEC, context, paths=(path,)) == PermissionDecision(
        PermissionOutcome.DENY, PermissionReason.PROTECTED_PATH
    )


@pytest.mark.parametrize(
    ("denied_path", "reason"),
    [("src_backup/main.py", PermissionReason.PATH_OUT_OF_SCOPE), ("src/.env.local", PermissionReason.PROTECTED_PATH)],
    ids=["out-of-scope", "protected"],
)
def test_policy_denies_entire_selection_when_one_path_is_denied(
    denied_path: str, reason: PermissionReason
) -> None:
    context = make_context(read_scopes=(PathScope("src", recursive=True),))
    allowed_paths = ("src/main.py", "src/config.py")

    assert POLICY.evaluate(READ_SPEC, context, paths=allowed_paths).outcome is PermissionOutcome.ALLOW
    assert POLICY.evaluate(READ_SPEC, context, paths=(*allowed_paths, denied_path)) == PermissionDecision(
        PermissionOutcome.DENY, reason
    )


def test_policy_requires_approval_for_authorized_write() -> None:
    context = make_context(
        capabilities=frozenset({Capability.REPOSITORY_WRITE}),
        write_scopes=(PathScope("src", recursive=True),),
    )

    assert POLICY.evaluate(WRITE_SPEC, context, paths=("src/main.py",)) == PermissionDecision(
        PermissionOutcome.APPROVAL_REQUIRED, PermissionReason.APPROVAL_REQUIRED
    )


@pytest.mark.parametrize(
    ("path", "reason"),
    [("README.md", PermissionReason.PATH_OUT_OF_SCOPE), ("src/.git/config", PermissionReason.PROTECTED_PATH)],
)
def test_policy_denies_unauthorized_write_before_approval(path: str, reason: PermissionReason) -> None:
    context = make_context(
        capabilities=frozenset({Capability.REPOSITORY_WRITE}),
        read_scopes=(ROOT_SCOPE,),
        write_scopes=(PathScope("src", recursive=True),),
    )

    assert POLICY.evaluate(WRITE_SPEC, context, paths=(path,)) == PermissionDecision(
        PermissionOutcome.DENY, reason
    )


def test_policy_requires_approval_for_authorized_read() -> None:
    spec = make_spec(Capability.REPOSITORY_READ, requires_approval=True)
    context = make_context(read_scopes=(PathScope("README.md"),))

    assert POLICY.evaluate(spec, context, paths=("README.md",)) == PermissionDecision(
        PermissionOutcome.APPROVAL_REQUIRED, PermissionReason.APPROVAL_REQUIRED
    )
