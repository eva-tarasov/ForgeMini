"""The real `ChatOpenAI` client against a fake OpenAI-compatible server, driving the real agent loop in Docker.

This is what "OpenAI-compatible" means in practice, and the closest thing to a live Kimi / GLM / Ollama run that works
without keys or internet: the server below speaks the wire format (POST /v1/chat/completions, `tools` in the request,
`tool_calls` in the response) and nothing else. If the provider preset (base URL, Chat Completions instead of the
Responses API, placeholder key) were wrong, the client would call a path this server does not have and the run fails.

It also shows who executes a tool: the model only RETURNS a `tool_calls` JSON; ForgeMini runs the command in the
sandbox and sends the output back as a `role: tool` message in the next request (asserted below).
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from app.bootstrap import build_chat_model
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.docker_sandbox import default_image, docker_available
from app.sandbox.model_provider import resolve_connection
from app.sandbox.runner import SandboxJob, SandboxRunner
from app.services.agent_runner import AgentRunRequest

pytestmark = pytest.mark.skipif(not asyncio.run(docker_available()), reason="no Docker daemon available")

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo-calc"
AVG_TEST = "python -m unittest tests.test_average"


def _call(name: str, args: dict) -> dict:
    return {"id": f"call_{uuid4().hex[:8]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


SCRIPT = [
    _call("bash", {"command": AVG_TEST}),
    _call("edit_file", {"path": "calc/ops.py", "old_str": "sum(numbers) // len(numbers)",
                        "new_str": "sum(numbers) / len(numbers)"}),
    _call("bash", {"command": AVG_TEST}),
    _call("finish", {"status": "done", "summary": "average used floor division"}),
]


class FakeProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []      # (path, headers, body)
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):                        # keep pytest output clean
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if self.path != "/v1/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return
                turn = len(provider.requests) - 1
                call = SCRIPT[min(turn, len(SCRIPT) - 1)]
                reply = {"id": f"cmpl-{turn}", "object": "chat.completion", "created": 0, "model": body["model"],
                         "choices": [{"index": 0, "finish_reason": "tool_calls",
                                      "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
                         "usage": {"prompt_tokens": 100 + turn, "completion_tokens": 10, "total_tokens": 110 + turn}}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def test_compatible_provider_drives_the_agent_to_a_fix(tmp_path):
    fake = FakeProvider()
    try:
        conn = resolve_connection(provider="ollama", model="qwen-fake", base_url=fake.url, api_key=None)
        model = build_chat_model(conn, timeout=30)

        async def resolve(_req):
            return SandboxJob(DEMO, default_image(), AVG_TEST)
        runner = SandboxRunner(model=model, resolve_job=resolve, artifacts=FileArtifactStore(tmp_path / "a"))
        req = AgentRunRequest(task_id=uuid4(), run_id=uuid4(), goal="average() is wrong", acceptance_criteria=())
        result = asyncio.run(runner.run(req))
    finally:
        fake.close()

    assert result.status == "ready_for_verification" and result.relevant_components == ("calc/ops.py",)
    paths = {p for p, _, _ in fake.requests}
    assert paths == {"/v1/chat/completions"}                    # never the Responses API, never another path
    _, headers, first = fake.requests[0]
    assert headers["authorization"] == "Bearer ollama"          # placeholder key: Ollama ignores it, the SDK needs it
    assert first["model"] == "qwen-fake"
    assert {t["function"]["name"] for t in first["tools"]} == {"bash", "read_file", "edit_file", "finish"}
    # The model never ran anything: ForgeMini did, and sent the output back as a `tool` message.
    _, _, second = fake.requests[1]
    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1 and tool_messages[0]["tool_call_id"] == SCRIPT[0]["id"]
    assert "FAILED" in tool_messages[0]["content"]              # the real red test output from the sandbox
