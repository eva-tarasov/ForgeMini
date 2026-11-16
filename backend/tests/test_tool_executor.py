import asyncio
import traceback
from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import model_validator

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.tools.contracts import ToolInput, ToolOutput, ToolRiskLevel, ToolSpec
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import PathSelector, RegisteredTool, ToolHandler, ToolRegistry


TOOL_NAME = "sample_read"
SYNTHETIC_SECRET = "synthetic-private-value"
READ_CONTEXT = PermissionContext(
    run_id=UUID(int=1),
    workspace_id=UUID(int=2),
    capabilities=frozenset({Capability.REPOSITORY_READ}),
)


class SampleInput(ToolInput):
    text: str


class SampleOutput(ToolOutput):
    size: int


class OtherOutput(ToolOutput):
    size: int


class DerivedOutput(SampleOutput):
    pass


def select_path(tool_input: SampleInput) -> tuple[str, ...]:
    return (tool_input.text,)


def make_executor(
    handler: ToolHandler,
    *,
    path_selector: PathSelector | None = None,
    synthetic: bool = True,
    bound_context: PermissionContext | None = None,
    **overrides: object,
) -> ToolExecutor:
    fields = {
        "name": TOOL_NAME,
        "version": 1,
        "description": "Synthetic tool for executor tests.",
        "input_schema": SampleInput,
        "output_schema": SampleOutput,
        "capability": Capability.REPOSITORY_READ,
        "risk_level": ToolRiskLevel.LOW,
        "timeout_ms": 5000,
        "side_effect": False,
        "requires_approval": False,
    }
    spec = ToolSpec.model_validate(fields | overrides)
    definition = RegisteredTool(
        spec, handler, path_selector=path_selector, synthetic=synthetic, bound_context=bound_context
    )
    return ToolExecutor(ToolRegistry([definition]))


@pytest.fixture
def forbidden_handler() -> ToolHandler:
    async def handler(_tool_input: ToolInput) -> ToolOutput:
        pytest.fail("A rejected execution must never invoke its handler")

    return handler


def assert_safe_error(error: ToolExecutionError, code: ToolErrorCode) -> None:
    assert error.code is code
    assert str(error) == code.value
    assert error.__suppress_context__
    assert SYNTHETIC_SECRET not in "".join(traceback.format_exception(error))


def test_executor_returns_validated_output() -> None:
    calls = []
    original = SampleOutput(size=3)

    async def handler(tool_input: ToolInput) -> ToolOutput:
        calls.append(tool_input)
        return original

    executor = make_executor(handler, version=2)
    result = asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT, version=2))

    assert calls == [SampleInput(text="abc")]
    assert type(calls[0]) is SampleInput
    assert type(result) is SampleOutput
    assert result == original
    assert result is not original


@pytest.mark.parametrize(
    ("name", "version", "code"),
    [
        ("missing", None, ToolErrorCode.UNKNOWN_TOOL),
        ("SAMPLE_READ", 1, ToolErrorCode.UNKNOWN_TOOL),
        (TOOL_NAME, 2, ToolErrorCode.VERSION_MISMATCH),
    ],
)
def test_executor_translates_lookup_errors(
    forbidden_handler: ToolHandler, name: str, version: int | None, code: ToolErrorCode
) -> None:
    executor = make_executor(forbidden_handler)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(name, {"text": "abc"}, context=READ_CONTEXT, version=version))

    assert_safe_error(exc_info.value, code)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("", {}), (1, {}), (None, {}), (TOOL_NAME, []), (TOOL_NAME, SampleInput(text="abc"))],
)
def test_executor_rejects_invalid_request_shape(
    forbidden_handler: ToolHandler, name: object, arguments: object
) -> None:
    executor = make_executor(forbidden_handler)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(name, arguments, context=READ_CONTEXT))

    assert exc_info.value.code is ToolErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 0, -1])
def test_executor_rejects_invalid_version(forbidden_handler: ToolHandler, version: object) -> None:
    executor = make_executor(forbidden_handler)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT, version=version))

    assert exc_info.value.code is ToolErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "arguments", [{"text": SYNTHETIC_SECRET.encode()}, {}, {"text": "abc", "extra": SYNTHETIC_SECRET}]
)
def test_executor_rejects_invalid_input_without_invoking_handler(
    forbidden_handler: ToolHandler, arguments: dict[str, object]
) -> None:
    executor = make_executor(forbidden_handler)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, arguments, context=READ_CONTEXT))

    assert_safe_error(exc_info.value, ToolErrorCode.INVALID_INPUT)


def test_executor_translates_unexpected_input_validation_failure(
    forbidden_handler: ToolHandler,
) -> None:
    class BrokenInput(SampleInput):
        @model_validator(mode="before")
        @classmethod
        def fail(cls, _arguments: object) -> object:
            raise RuntimeError(SYNTHETIC_SECRET)

    executor = make_executor(forbidden_handler, input_schema=BrokenInput)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert_safe_error(exc_info.value, ToolErrorCode.INTERNAL_ERROR)


def test_executor_blocks_write_tool(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(
        forbidden_handler,
        path_selector=select_path,
        synthetic=False,
        capability=Capability.REPOSITORY_WRITE,
        risk_level=ToolRiskLevel.HIGH,
        side_effect=True,
        requires_approval=True,
    )
    context = replace(
        READ_CONTEXT,
        capabilities=frozenset({Capability.REPOSITORY_WRITE}),
        write_scopes=(PathScope("src", recursive=True),),
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/module.py"}, context=context))

    assert exc_info.value.code is ToolErrorCode.EXECUTION_BLOCKED


def test_executor_blocks_read_tool_requiring_approval(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(forbidden_handler, requires_approval=True)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert exc_info.value.code is ToolErrorCode.EXECUTION_BLOCKED


def test_executor_executes_authorized_logical_path() -> None:
    calls = []

    def selector(tool_input: SampleInput) -> tuple[str, ...]:
        calls.append(("selector", tool_input))
        return select_path(tool_input)

    async def handler(tool_input: SampleInput) -> SampleOutput:
        calls.append(("handler", tool_input))
        return SampleOutput(size=len(tool_input.text))

    path = "src/module.py"
    context = replace(READ_CONTEXT, read_scopes=(PathScope("src", recursive=True),))
    executor = make_executor(handler, path_selector=selector, synthetic=False)

    output = asyncio.run(executor.execute(TOOL_NAME, {"text": path}, context=context))

    assert output == SampleOutput(size=len(path))
    assert calls == [("selector", SampleInput(text=path)), ("handler", SampleInput(text=path))]
    assert calls[0][1] is calls[1][1]


@pytest.mark.parametrize("context_kwargs", [{}, {"context": {}}], ids=["missing", "invalid"])
def test_executor_rejects_missing_or_invalid_context(
    forbidden_handler: ToolHandler, context_kwargs: dict[str, object]
) -> None:
    def selector(_tool_input: ToolInput) -> tuple[str, ...]:
        pytest.fail("The selector requires a trusted permission context")

    executor = make_executor(forbidden_handler, path_selector=selector, synthetic=False)
    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/module.py"}, **context_kwargs))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


def test_executor_rejects_missing_capability(forbidden_handler: ToolHandler) -> None:
    context = replace(READ_CONTEXT, capabilities=frozenset())
    executor = make_executor(forbidden_handler)

    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(replace(READ_CONTEXT, run_id=UUID(int=3)), id="other-run"),
        pytest.param(replace(READ_CONTEXT, workspace_id=UUID(int=3)), id="other-workspace"),
        pytest.param(replace(READ_CONTEXT), id="equal-but-distinct"),
    ],
)
def test_executor_rejects_mismatched_binding_before_selector(
    forbidden_handler: ToolHandler, context: PermissionContext
) -> None:
    def selector(_tool_input: ToolInput) -> tuple[str, ...]:
        pytest.fail("A mismatched bound context must be rejected before selection")

    executor = make_executor(
        forbidden_handler, path_selector=selector, synthetic=False, bound_context=READ_CONTEXT
    )
    assert context is not READ_CONTEXT
    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "."}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


def test_executor_rejects_missing_access_declaration(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(forbidden_handler, synthetic=False)
    context = replace(READ_CONTEXT, read_scopes=(PathScope(".", recursive=True),))

    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/module.py"}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize("selection", [(), None, "src/module.py", ["src/module.py"], (4,), ("../x",)])
def test_executor_rejects_empty_or_malformed_path_selection(
    forbidden_handler: ToolHandler, selection: object
) -> None:
    executor = make_executor(forbidden_handler, path_selector=lambda _: selection, synthetic=False)
    context = replace(READ_CONTEXT, read_scopes=(PathScope(".", recursive=True),))

    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/module.py"}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


def test_executor_rejects_unauthorized_path(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(forbidden_handler, path_selector=select_path, synthetic=False)
    context = replace(READ_CONTEXT, read_scopes=(PathScope("src", recursive=True),))

    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src_backup/module.py"}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


def test_executor_rejects_protected_path_under_root_grant(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(forbidden_handler, path_selector=select_path, synthetic=False)
    context = replace(READ_CONTEXT, read_scopes=(PathScope(".", recursive=True),))

    with pytest.raises(ToolExecutionError, match="^permission_denied$") as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/.GIT/config"}, context=context))

    assert exc_info.value.code is ToolErrorCode.PERMISSION_DENIED


def test_executor_translates_selector_exception_safely(forbidden_handler: ToolHandler) -> None:
    def selector(_tool_input: ToolInput) -> tuple[str, ...]:
        raise RuntimeError(SYNTHETIC_SECRET)

    executor = make_executor(forbidden_handler, path_selector=selector, synthetic=False)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "src/module.py"}, context=READ_CONTEXT))

    assert_safe_error(exc_info.value, ToolErrorCode.INTERNAL_ERROR)


def test_executor_rejects_permission_fields_in_tool_arguments(forbidden_handler: ToolHandler) -> None:
    executor = make_executor(forbidden_handler)
    arguments = {
        "text": "abc",
        "context": READ_CONTEXT,
        "capabilities": ["repository.read"],
        "paths": ["."],
        "approved": True,
    }

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, arguments))

    assert_safe_error(exc_info.value, ToolErrorCode.INVALID_INPUT)


def test_executor_rejects_synchronous_handler_without_invoking_it() -> None:
    def handler(_tool_input: ToolInput) -> ToolOutput:
        pytest.fail("A synchronous handler must never be invoked")

    executor = make_executor(handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert exc_info.value.code is ToolErrorCode.INVALID_HANDLER


def test_executor_accepts_async_callable_object() -> None:
    class Handler:
        async def __call__(self, tool_input: SampleInput) -> SampleOutput:
            return SampleOutput(size=len(tool_input.text))

    executor = make_executor(Handler())

    assert asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT)) == SampleOutput(size=3)


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_executor_translates_handler_error_without_retry(error_type: type[Exception]) -> None:
    calls = []

    async def handler(tool_input: ToolInput) -> ToolOutput:
        calls.append(tool_input)
        raise error_type(SYNTHETIC_SECRET)

    executor = make_executor(handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert calls == [SampleInput(text="abc")]
    assert_safe_error(exc_info.value, ToolErrorCode.HANDLER_FAILED)


@pytest.mark.parametrize("suppress_cancellation", [False, True], ids=["cleanup", "late-return"])
def test_executor_times_out_and_cleans_up(suppress_cancellation: bool) -> None:
    async def check() -> None:
        release, cleaned = asyncio.Event(), asyncio.Event()
        tasks = []

        async def handler(_tool_input: ToolInput) -> ToolOutput:
            tasks.append(asyncio.current_task())
            try:
                await release.wait()
            except asyncio.CancelledError:
                if suppress_cancellation:
                    return SampleOutput(size=3)
                raise
            finally:
                await asyncio.sleep(0)
                cleaned.set()
            return SampleOutput(size=3)

        executor = make_executor(handler, timeout_ms=20)
        async with asyncio.timeout(3):
            with pytest.raises(ToolExecutionError) as exc_info:
                await executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT)

        assert exc_info.value.code is ToolErrorCode.TOOL_TIMEOUT
        assert cleaned.is_set()
        assert tasks == [asyncio.current_task()]
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(check())


def test_executor_rejects_return_after_monotonic_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()

        async def handler(_tool_input: ToolInput) -> ToolOutput:
            nonlocal now
            # Advance the clock without yielding to the timeout callback.
            now += 2
            return SampleOutput(size=3)

        executor = make_executor(handler, timeout_ms=1000)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: now)
            with pytest.raises(ToolExecutionError) as exc_info:
                await executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT)

        assert exc_info.value.code is ToolErrorCode.TOOL_TIMEOUT

    asyncio.run(check())


def test_executor_classifies_exception_after_monotonic_deadline_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()

        async def handler(_tool_input: ToolInput) -> ToolOutput:
            nonlocal now
            now += 2
            raise RuntimeError(SYNTHETIC_SECRET)

        executor = make_executor(handler, timeout_ms=1000)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: now)
            with pytest.raises(ToolExecutionError) as exc_info:
                await executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT)

        assert_safe_error(exc_info.value, ToolErrorCode.TOOL_TIMEOUT)

    asyncio.run(check())


def test_executor_propagates_external_cancellation() -> None:
    async def check() -> None:
        started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellations = []

        async def handler(_tool_input: ToolInput) -> ToolOutput:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
                raise
            finally:
                await asyncio.sleep(0)
                cleaned.set()
            return SampleOutput(size=3)

        executor = make_executor(handler, timeout_ms=30_000)
        task = asyncio.create_task(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel("external cancellation")
            done, _ = await asyncio.wait({task}, timeout=3)
            assert task in done
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await task
            assert len(cancellations) == 1
            assert exc_info.value is cancellations[0]
            assert cleaned.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize("raise_after_cancellation", [False, True], ids=["returns-output", "raises-error"])
def test_executor_preserves_swallowed_external_cancellation(raise_after_cancellation: bool) -> None:
    async def check() -> None:
        started, release, swallowed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(_tool_input: ToolInput) -> ToolOutput:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                swallowed.set()
                if raise_after_cancellation:
                    raise RuntimeError(SYNTHETIC_SECRET)
            return SampleOutput(size=3)

        executor = make_executor(handler, timeout_ms=30_000)
        task = asyncio.create_task(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=3)
            assert task in done
            assert swallowed.is_set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert task.cancelling() == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("abc", id="string"),
        pytest.param(None, id="none"),
        pytest.param({"size": 3}, id="dict"),
        pytest.param(OtherOutput(size=3), id="unrelated-model"),
        pytest.param(DerivedOutput(size=3), id="subclass"),
        pytest.param(SampleOutput.model_construct(size="3"), id="invalid-constructed-value"),
        pytest.param(SampleOutput.model_construct(), id="missing-constructed-field"),
    ],
)
def test_executor_rejects_invalid_output(output: object) -> None:
    async def handler(_tool_input: ToolInput) -> object:
        return output

    executor = make_executor(handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert exc_info.value.code is ToolErrorCode.INVALID_OUTPUT


@pytest.mark.parametrize("updates", [{"size": SYNTHETIC_SECRET}, {"unexpected": SYNTHETIC_SECRET}])
def test_executor_rejects_invalid_copied_output(updates: dict[str, object]) -> None:
    async def handler(_tool_input: ToolInput) -> ToolOutput:
        return SampleOutput(size=3).model_copy(update=updates)

    executor = make_executor(handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert_safe_error(exc_info.value, ToolErrorCode.INVALID_OUTPUT)


def test_executor_rejects_output_with_extra_storage() -> None:
    async def handler(_tool_input: ToolInput) -> ToolOutput:
        output = SampleOutput(size=3)
        object.__setattr__(output, "__pydantic_extra__", {"unexpected": SYNTHETIC_SECRET})
        return output

    executor = make_executor(handler)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(executor.execute(TOOL_NAME, {"text": "abc"}, context=READ_CONTEXT))

    assert exc_info.value.code is ToolErrorCode.INVALID_OUTPUT
    assert str(exc_info.value) == "invalid_output"
