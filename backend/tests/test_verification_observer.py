import asyncio
import os
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.capabilities import Capability
from app.permissions.context import PathScope
from app.services.verification_contract import (
    FileDigestCheck,
    ObservationIssue,
    VerificationBinding,
    VerificationContractError,
    VerificationPlan,
    VerificationVerdict,
    aggregate_verification,
    fingerprint_criteria,
)
from app.services.verification_observer import TrustedFixtureAccess, observe
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.repository import filesystem as fs


def scenario(root, checks, *, criteria=("Fixture is correct",), scopes=(PathScope(".", recursive=True),)):
    binding = VerificationBinding(UUID(int=1), UUID(int=2), UUID(int=3), "fixture-v1")
    plan = VerificationPlan(
        plan_id=UUID(int=4),
        binding=binding,
        criterion_digests=fingerprint_criteria(criteria),
        checks=tuple(
            FileDigestCheck(UUID(int=index), criterion, path, sha256(expected).hexdigest())
            for index, (criterion, path, expected) in enumerate(checks, start=5)
        ),
    )
    access = TrustedFixtureAccess(
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        binding=binding,
        root=root,
        read_scopes=scopes,
        acceptance_criteria=criteria,
    )
    return plan, access


@pytest.fixture
def descriptor_trace(monkeypatch):
    original = fs._open_fd
    opened = []
    active = set()

    @contextmanager
    def track(path, flags, *, parent=None):
        with original(path, flags, parent=parent) as fd:
            opened.append((path, flags, parent))
            active.add(fd)
            try:
                yield fd
            finally:
                active.remove(fd)
        with pytest.raises(OSError):
            os.fstat(fd)

    monkeypatch.setattr(fs, "_open_fd", track)
    return opened, active


@pytest.mark.parametrize("matches", [True, False], ids=["pass", "fail"])
def test_observer_hashes_exact_utf8_bytes(tmp_path, matches):
    content = " Привет\r\nlast\n".encode("utf-8")
    (tmp_path / "one.txt").write_bytes(content)
    expected = content if matches else content.replace(b"\r\n", b"\n")
    plan, access = scenario(tmp_path, [(0, "one.txt", expected)])
    evidence = asyncio.run(observe(plan, access))

    assert (evidence.plan_id, evidence.binding, evidence.plan_fingerprint) == (
        plan.plan_id, plan.binding, plan.fingerprint,
    )
    assert evidence.observations[0].observed_sha256 == sha256(content).hexdigest()
    assert aggregate_verification(plan, evidence) is (
        VerificationVerdict.PASS if matches else VerificationVerdict.FAIL
    )


def test_observer_returns_all_checks_in_order_and_opens_shared_path_once(tmp_path, descriptor_trace):
    (tmp_path / "shared.txt").write_bytes(b"shared")
    (tmp_path / "other.txt").write_bytes(b"other")
    plan, access = scenario(
        tmp_path,
        [(1, "shared.txt", b"shared"), (0, "other.txt", b"other"), (0, "shared.txt", b"shared")],
        criteria=("First", "Second"),
    )
    evidence = asyncio.run(observe(plan, access))

    assert tuple(item.check_id for item in evidence.observations) == tuple(check.check_id for check in plan.checks)
    assert evidence.observations[0].observed_sha256 == evidence.observations[2].observed_sha256
    assert aggregate_verification(plan, evidence) is VerificationVerdict.PASS
    opened, active = descriptor_trace
    assert [path for path, flags, _ in opened if not flags & os.O_DIRECTORY] == ["shared.txt", "other.txt"]
    assert not active


def test_observer_missing_file_is_inconclusive(tmp_path):
    plan, access = scenario(tmp_path, [(0, "missing.txt", b"expected")])
    evidence = asyncio.run(observe(plan, access))

    assert evidence.observations[0].issue is ObservationIssue.READ_ERROR
    assert evidence.observations[0].observed_sha256 is None
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE


@pytest.mark.parametrize("grant_public", [False, True], ids=["no-grants", "exact-grant"])
def test_observer_obeys_independent_read_scopes(tmp_path, descriptor_trace, grant_public):
    (tmp_path / "public.txt").write_bytes(b"public")
    (tmp_path / "private.txt").write_bytes(b"synthetic secret")
    scopes = (PathScope("public.txt"),) if grant_public else ()
    plan, access = scenario(
        tmp_path, [(0, "private.txt", b"synthetic secret"), (0, "public.txt", b"public")], scopes=scopes,
    )
    evidence = asyncio.run(observe(plan, access))
    opened, _ = descriptor_trace

    assert evidence.observations[0].issue is ObservationIssue.READ_ERROR
    assert [path for path, flags, _ in opened if not flags & os.O_DIRECTORY] == (
        ["public.txt"] if grant_public else []
    )
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE


@pytest.mark.parametrize("kind", ["symlink", "hard-link", "intermediate-symlink"])
def test_observer_refuses_links_before_opening_target(tmp_path, descriptor_trace, kind):
    root = tmp_path / "fixture"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    entry = root / "entry"
    if kind == "symlink":
        entry.symlink_to(outside)
    elif kind == "hard-link":
        os.link(outside, entry)
    else:
        entry.symlink_to(tmp_path, target_is_directory=True)
    path = "entry/outside.txt" if kind == "intermediate-symlink" else "entry"
    plan, access = scenario(root, [(0, path, b"outside")])
    evidence = asyncio.run(observe(plan, access))

    assert evidence.observations[0].issue is ObservationIssue.READ_ERROR
    opened, _ = descriptor_trace
    assert opened
    assert all(opened_path in (root.resolve(), ".") and flags & os.O_DIRECTORY for opened_path, flags, _ in opened)
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE


@pytest.mark.parametrize("kind", ["missing-root", "symlink-root"])
def test_observer_unreadable_root_is_inconclusive(tmp_path, descriptor_trace, kind):
    root = tmp_path / "fixture"
    if kind == "symlink-root":
        root.symlink_to(tmp_path, target_is_directory=True)
    plan, access = scenario(root, [(0, "one.txt", b"expected")])
    evidence = asyncio.run(observe(plan, access))

    assert evidence.observations[0].issue is ObservationIssue.READ_ERROR
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE
    assert descriptor_trace[0] == []


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/absolute.txt", ".GiT/config", "sub/.env.test"])
def test_observer_plan_rejects_unsafe_paths_without_opening_root(tmp_path, descriptor_trace, path):
    with pytest.raises(VerificationContractError, match="^invalid_verification_path$"):
        plan, access = scenario(tmp_path, [(0, path, b"expected")])
        asyncio.run(observe(plan, access))
    assert descriptor_trace[0] == []


@pytest.mark.parametrize(
    "content", [b"\xffinvalid", b"x" * (fs.MAX_FILE_BYTES + 1)], ids=["invalid-utf8", "file-budget"],
)
def test_observer_refuses_unsupported_or_oversized_text(tmp_path, content):
    (tmp_path / "one.txt").write_bytes(content)
    plan, access = scenario(tmp_path, [(0, "one.txt", content)])
    evidence = asyncio.run(observe(plan, access))

    assert evidence.observations[0].issue is ObservationIssue.READ_ERROR
    assert evidence.observations[0].observed_sha256 is None
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE


@pytest.mark.parametrize("invalid_utf8", [False, True], ids=["successful-reads", "failed-reads"])
def test_observer_enforces_total_byte_budget_before_opening_next_file(
    tmp_path, descriptor_trace, monkeypatch, invalid_utf8,
):
    chunk = (b"\xff" if invalid_utf8 else b"x") * (60 * 1024)
    checks = [(0, f"file-{index}.txt", chunk) for index in range(5)]
    for _, path, content in checks:
        (tmp_path / path).write_bytes(content)
    plan, access = scenario(tmp_path, checks)
    original_read = os.read
    bytes_read = 0

    def count_read(fd, size):
        nonlocal bytes_read
        result = original_read(fd, size)
        bytes_read += len(result)
        return result

    monkeypatch.setattr(os, "read", count_read)
    evidence = asyncio.run(observe(plan, access))

    expected_opens = 3 if invalid_utf8 else 4
    assert bytes_read == expected_opens * len(chunk) <= 256 * 1024
    opened, _ = descriptor_trace
    assert [path for path, flags, _ in opened if not flags & os.O_DIRECTORY] == [
        path for _, path, _ in checks[:expected_opens]
    ]
    assert evidence.observations[-1].issue is ObservationIssue.READ_ERROR
    assert aggregate_verification(plan, evidence) is VerificationVerdict.INCONCLUSIVE


def test_observer_reuses_budget_after_small_successful_reads(tmp_path):
    checks = [(0, f"small-{i}.txt", b"x") for i in range(8)]
    for _, path, content in checks:
        (tmp_path / path).write_bytes(content)
    plan, access = scenario(tmp_path, checks)
    evidence = asyncio.run(observe(plan, access))

    assert aggregate_verification(plan, evidence) is VerificationVerdict.PASS


@pytest.mark.parametrize(
    "mismatch",
    ["plan-id", "fingerprint", "run", "task", "workspace", "revision", "criteria-order", "criteria-subset"],
)
def test_observer_rejects_mismatched_binding_before_touching_root(tmp_path, monkeypatch, mismatch):
    plan, access = scenario(
        tmp_path, [(0, "one.txt", b"expected"), (1, "two.txt", b"expected")], criteria=("First", "Second"),
    )
    if mismatch == "plan-id":
        access = replace(access, plan_id=UUID(int=42))
    elif mismatch == "fingerprint":
        plan = replace(plan, checks=(plan.checks[0], replace(plan.checks[1], path="different.txt")))
    elif mismatch.startswith("criteria-"):
        snapshot = ("Second", "First") if mismatch == "criteria-order" else ("First",)
        access = replace(access, acceptance_criteria=snapshot)
    elif mismatch == "revision":
        access = replace(access, binding=replace(access.binding, baseline_revision="other-revision"))
    else:
        access = replace(access, binding=replace(access.binding, **{f"{mismatch}_id": UUID(int=42)}))

    def unexpected_lstat(*args, **kwargs):
        pytest.fail("Mismatched metadata must be rejected before fixture access")

    monkeypatch.setattr(Path, "lstat", unexpected_lstat)
    with pytest.raises(VerificationContractError, match="^verification_access_mismatch$"):
        asyncio.run(observe(plan, access))


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_id", "bad"),
        ("plan_fingerprint", b"bad"),
        ("binding", None),
        ("root", Path("relative")),
        ("root", "/tmp/host-fixture"),
        ("root", Path("/tmp/fixture\0root")),
        ("root", Path("/tmp/fixture\ud800root")),
        ("read_scopes", [PathScope(".")]),
        ("read_scopes", ("bad",)),
        ("acceptance_criteria", ["Fixture is correct"]),
    ],
)
def test_observer_validates_trusted_access_before_touching_root(tmp_path, monkeypatch, field, value):
    plan, access = scenario(tmp_path, [(0, "one.txt", b"expected")])

    def unexpected_lstat(*args, **kwargs):
        pytest.fail("Invalid access must be rejected before fixture access")

    monkeypatch.setattr(Path, "lstat", unexpected_lstat)
    with pytest.raises(VerificationContractError, match="^invalid_verification_(access|criteria)$"):
        asyncio.run(observe(plan, replace(access, **{field: value})))


def test_observer_requires_typed_plan_and_access(tmp_path, descriptor_trace):
    plan, access = scenario(tmp_path, [(0, "one.txt", b"expected")])
    with pytest.raises(VerificationContractError, match="^invalid_verification_plan$"):
        asyncio.run(observe({"plan_id": plan.plan_id}, access))
    with pytest.raises(VerificationContractError, match="^invalid_verification_access$"):
        asyncio.run(observe(plan, {"root": tmp_path, "plan_id": plan.plan_id}))
    assert descriptor_trace[0] == []


def test_observer_reads_fresh_content_across_invocations(tmp_path):
    file = tmp_path / "one.txt"
    file.write_bytes(b"first")
    plan, access = scenario(tmp_path, [(0, "one.txt", b"first")])
    first = asyncio.run(observe(plan, access))
    file.write_bytes(b"second")
    second = asyncio.run(observe(plan, access))

    assert first.observations[0].observed_sha256 == sha256(b"first").hexdigest()
    assert second.observations[0].observed_sha256 == sha256(b"second").hexdigest()
    assert aggregate_verification(plan, first) is VerificationVerdict.PASS
    assert aggregate_verification(plan, second) is VerificationVerdict.FAIL


def test_observer_propagates_cancellation_and_closes_real_reader_fds(tmp_path, descriptor_trace, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "one.txt").write_bytes(b"expected")
    plan, access = scenario(tmp_path, [(0, "nested/one.txt", b"expected")])
    original = os.read
    intercepted_fds = []
    cancelled = asyncio.CancelledError("fixture read cancelled")

    def interrupt_read(fd, size):
        if fd in descriptor_trace[1]:
            intercepted_fds.extend(descriptor_trace[1])
            raise cancelled
        return original(fd, size)

    monkeypatch.setattr(os, "read", interrupt_read)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(observe(plan, access))
    assert caught.value is cancelled
    assert len(intercepted_fds) == 3
    assert not descriptor_trace[1]
    for fd in intercepted_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_observer_delivers_external_cancellation_before_next_read(tmp_path, descriptor_trace, monkeypatch):
    (tmp_path / "first.txt").write_bytes(b"first")
    (tmp_path / "second.txt").write_bytes(b"second")
    plan, access = scenario(tmp_path, [(0, "first.txt", b"first"), (0, "second.txt", b"second")])
    original = os.read

    def queue_cancel(fd, size):
        result = original(fd, size)
        if result and fd in descriptor_trace[1]:
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel, "external cancellation")
        return result

    async def run():
        task = asyncio.create_task(observe(plan, access))
        with pytest.raises(asyncio.CancelledError, match="external cancellation"):
            await task

    monkeypatch.setattr(os, "read", queue_cancel)
    asyncio.run(run())
    opened, active = descriptor_trace
    assert [path for path, flags, _ in opened if not flags & os.O_DIRECTORY] == ["first.txt"]
    assert not active


def test_observer_returns_payload_free_observations_and_errors(tmp_path, caplog):
    payload = b"synthetic credential SECRET_10B"
    (tmp_path / "fixture.txt").write_bytes(payload)
    plan, access = scenario(tmp_path, [(0, "fixture.txt", payload), (0, "missing.txt", payload)])
    evidence = asyncio.run(observe(plan, access))
    with pytest.raises(VerificationContractError) as caught:
        asyncio.run(observe(plan, replace(access, acceptance_criteria=(payload.decode(),))))

    diagnostics = repr(access) + repr(evidence) + str(caught.value) + caplog.text
    for value in (str(tmp_path), payload.decode(), "fixture.txt", sha256(payload).hexdigest()):
        assert value not in diagnostics
    assert evidence.observations[1].issue is ObservationIssue.READ_ERROR


def test_observer_uses_only_read_executor_and_has_no_write_sql_or_llm_side_effects(
    tmp_path, descriptor_trace, monkeypatch,
):
    from langchain_openai import ChatOpenAI
    from sqlalchemy.ext.asyncio import AsyncSession

    file = tmp_path / "one.txt"
    file.write_bytes(b"fixture")
    before = file.stat()
    plan, access = scenario(tmp_path, [(0, "one.txt", b"fixture")])
    calls = []
    original = ToolExecutor.execute

    def unexpected(*args, **kwargs):
        pytest.fail("Observer must not instantiate an SQL session or LLM")

    async def trace_execute(self, name, arguments, **kwargs):
        calls.append((name, arguments, kwargs["context"]))
        return await original(self, name, arguments, **kwargs)

    monkeypatch.setattr(AsyncSession, "__init__", unexpected)
    monkeypatch.setattr(ChatOpenAI, "__init__", unexpected)
    monkeypatch.setattr(ToolExecutor, "execute", trace_execute)
    evidence = asyncio.run(observe(plan, access))
    after = file.stat()

    assert aggregate_verification(plan, evidence) is VerificationVerdict.PASS
    [(name, arguments, context)] = calls
    assert (name, arguments) == ("read_file", {"path": "one.txt"})
    assert (context.run_id, context.workspace_id) == (plan.binding.run_id, plan.binding.workspace_id)
    assert context.capabilities == frozenset({Capability.REPOSITORY_READ})
    assert context.read_scopes == access.read_scopes and context.write_scopes == ()
    assert all(flags & (os.O_ACCMODE | os.O_CREAT | os.O_TRUNC) == os.O_RDONLY for _, flags, _ in descriptor_trace[0])
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert file.read_bytes() == b"fixture"
    assert list(tmp_path.iterdir()) == [file]


def test_observer_does_not_mask_unexpected_executor_error(tmp_path, monkeypatch):
    plan, access = scenario(tmp_path, [(0, "one.txt", b"expected")])

    async def fail_execution(*args, **kwargs):
        raise ToolExecutionError(ToolErrorCode.INVALID_OUTPUT)

    monkeypatch.setattr(ToolExecutor, "execute", fail_execution)
    with pytest.raises(VerificationContractError, match="^verification_reader_contract_error$"):
        asyncio.run(observe(plan, access))
