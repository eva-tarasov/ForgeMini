import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.sandbox.agent_loop import LoopBudget, run_agent_loop
from app.sandbox.docker_sandbox import ExecResult
from app.sandbox.tools import Toolbox
from sandbox_support import FakeSandbox, ScriptedModel, text_turn, tool_turn


def run(coro):
    return asyncio.run(coro)


def box(**files):
    sb = FakeSandbox({f"/workspace/{k}": v for k, v in files.items()})
    return sb, Toolbox(sb)  # type: ignore[arg-type]


# ----------------------------------------------------------------- tools
def test_read_file_numbers_lines_and_reports_range():
    sb, tb = box(**{"a.py": "x\ny\nz\n"})
    r = run(tb.call("read_file", {"path": "a.py", "start_line": 2, "end_line": 3}))
    assert not r.is_error and "    2\ty" in r.content and "    3\tz" in r.content and "[lines 2-3 of 3]" in r.content


def test_read_missing_file_is_a_tool_error_not_an_exception():
    _, tb = box()
    r = run(tb.call("read_file", {"path": "nope.py"}))
    assert r.is_error and "No such file" in r.content


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "a/../../x", "", None, 5])
def test_paths_outside_workspace_are_rejected(path):
    _, tb = box()
    assert run(tb.call("read_file", {"path": path})).is_error
    assert run(tb.call("edit_file", {"path": path, "old_str": "", "new_str": "x"})).is_error


def test_edit_replaces_exactly_one_occurrence():
    sb, tb = box(**{"a.py": "return a - b\n"})
    r = run(tb.call("edit_file", {"path": "a.py", "old_str": "a - b", "new_str": "a + b"}))
    assert not r.is_error and sb.files["/workspace/a.py"] == b"return a + b\n"


def test_edit_errors_teach_the_model_what_to_do():
    sb, tb = box(**{"a.py": "x = 1\nx = 1\n"})
    nf = run(tb.call("edit_file", {"path": "a.py", "old_str": "nope", "new_str": "y"}))
    many = run(tb.call("edit_file", {"path": "a.py", "old_str": "x = 1", "new_str": "y"}))
    assert nf.is_error and "not found" in nf.content and "line numbers" in nf.content
    assert many.is_error and "2 places" in many.content
    assert sb.files["/workspace/a.py"] == b"x = 1\nx = 1\n"            # a failed edit changes nothing


def test_edit_with_empty_old_str_creates_but_never_overwrites():
    sb, tb = box(**{"a.py": "1"})
    assert not run(tb.call("edit_file", {"path": "new/b.py", "old_str": "", "new_str": "hi"})).is_error
    assert sb.files["/workspace/new/b.py"] == b"hi"
    assert run(tb.call("edit_file", {"path": "a.py", "old_str": "", "new_str": "x"})).is_error


def test_bash_reports_exit_code_and_timeout():
    sb, tb = box()
    sb.exec_result = ExecResult(1, "boom", timed_out=False)
    assert run(tb.call("bash", {"command": "false"})).content.startswith("exit code: 1\nboom")
    sb.exec_result = ExecResult(124, "", timed_out=True)
    assert "TIMEOUT" in run(tb.call("bash", {"command": "sleep 999"})).content


def test_unknown_tool_and_bad_args_are_errors():
    _, tb = box()
    assert "unknown tool" in run(tb.call("rm_rf", {})).content
    assert run(tb.call("bash", {"command": 5})).is_error
    assert run(tb.call("bash", "not a dict")).is_error
    assert run(tb.call("finish", {"status": "great", "summary": "x"})).is_error


# ------------------------------------------------------------------ loop
def loop(model, sb=None, **kw):
    return run(run_agent_loop(model, sb or FakeSandbox(), goal="fix it", test_command="make test", **kw))


def test_finish_done_is_only_a_claim_with_usage_counted():
    m = ScriptedModel([tool_turn("bash", {"command": "make test"}),
                       tool_turn("finish", {"status": "done", "summary": "fixed"})])
    out = loop(m)
    assert (out.status, out.summary, out.steps, out.tool_calls) == ("claimed_done", "fixed", 2, 2)
    assert out.input_tokens == 200 and out.output_tokens == 20
    assert [e["kind"] for e in out.transcript][-1] == "finish"


def test_cannot_fix_is_reported_with_reason():
    out = loop(ScriptedModel([tool_turn("finish", {"status": "cannot_fix", "summary": "contradicts test"})]))
    assert (out.status, out.summary) == ("cannot_fix", "contradicts test")


def test_model_SEES_tool_errors_and_can_recover():
    """The core fix over M1, where a tool error aborted the run and the model never saw it."""
    sb = FakeSandbox({"/workspace/a.py": "x = 1\n"})
    m = ScriptedModel([
        tool_turn("edit_file", {"path": "a.py", "old_str": "WRONG", "new_str": "x = 2"}),
        tool_turn("edit_file", {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}),
        tool_turn("finish", {"status": "done", "summary": "ok"}),
    ])
    out = loop(m, sb)
    assert out.status == "claimed_done" and sb.files["/workspace/a.py"] == b"x = 2\n"
    second_call_messages = m.seen[1]
    err = [x for x in second_call_messages if isinstance(x, ToolMessage)][-1]
    assert err.status == "error" and "not found" in err.content


def test_step_budget_stops_an_endless_agent():
    m = ScriptedModel([tool_turn("bash", {"command": "ls"}) for _ in range(10)])
    out = loop(m, budget=LoopBudget(max_steps=3))
    assert (out.status, out.steps) == ("budget_exhausted", 3)


def test_plain_text_replies_are_nudged_then_give_up():
    m = ScriptedModel([text_turn("I think it is fine")] * 5)
    out = loop(m, budget=LoopBudget(max_nudges=2))
    assert out.status == "no_finish" and out.steps == 3          # 1 + 2 nudges, then stop
    assert "Use a tool" in m.seen[1][-1].content


def test_unparsable_tool_call_arguments_are_reported_back():
    broken = AIMessage(content="", invalid_tool_calls=[
        {"name": "bash", "args": "{oops", "id": "bad1", "error": "JSONDecodeError", "type": "invalid_tool_call"}])
    m = ScriptedModel([broken, tool_turn("finish", {"status": "done", "summary": "ok"})])
    out = loop(m)
    assert out.status == "claimed_done"
    reply = [x for x in m.seen[1] if isinstance(x, ToolMessage)][0]
    assert reply.tool_call_id == "bad1" and reply.status == "error" and "invalid arguments" in reply.content


def test_events_are_emitted_in_order():
    events: list[str] = []

    async def sink(kind, data):
        events.append(kind)
    loop(ScriptedModel([tool_turn("bash", {"command": "ls"}),
                        tool_turn("finish", {"status": "done", "summary": "s"})]), emit=sink)
    assert events == ["model_response", "tool_result", "model_response", "tool_result", "finish"]
