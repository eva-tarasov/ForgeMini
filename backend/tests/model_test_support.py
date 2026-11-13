import socket

import pytest
from langsmith import tracing_context


TRACING_OFF = dict.fromkeys(
    (
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_HANDLER",
    ),
    "false",
)


@pytest.fixture(autouse=True)
def offline_model_tests(monkeypatch, request):
    for name, value in getattr(request, "param", {}).items():
        monkeypatch.setenv(name, value)
    for name, value in TRACING_OFF.items():
        monkeypatch.setenv(name, value)

    def reject_network(*args, **kwargs):
        pytest.fail("Model tests must not connect to the network")

    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)

    # LangSmith can cache environment lookups before this fixture runs.
    with tracing_context(enabled=False, parent=False):
        yield
