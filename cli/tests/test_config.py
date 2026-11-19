import pytest

from forge_cli.config import resolve_base_url


def test_base_url_default(monkeypatch):
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)

    assert resolve_base_url() == "http://127.0.0.1:8000"


def test_base_url_from_environment(monkeypatch):
    monkeypatch.setenv("FORGE_BASE_URL", "https://forge.example.invalid/api")

    assert resolve_base_url() == "https://forge.example.invalid/api"


def test_base_url_flag_takes_priority(monkeypatch):
    monkeypatch.setenv("FORGE_BASE_URL", "https://forge.example.invalid/api")

    assert resolve_base_url("http://[::1]:9000/") == "http://[::1]:9000/"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://forge.example.invalid",
        "https:///api",
        "https://user:password@forge.example.invalid",
        "https://forge.example.invalid?token=example",
        "https://forge.example.invalid#fragment",
        "https://forge.example.invalid:65536",
        "https://[::1]extra",
        "https://forge.example.invalid/\napi",
        "https://forge.example.invalid\\api",
    ],
)
def test_invalid_base_url_flag_does_not_fall_back(monkeypatch, value):
    monkeypatch.setenv("FORGE_BASE_URL", "http://127.0.0.1:9000")

    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        resolve_base_url(value)
