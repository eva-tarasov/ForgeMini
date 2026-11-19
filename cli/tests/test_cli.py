import pytest
from typer.testing import CliRunner

from forge_cli.main import app

runner = CliRunner()


@pytest.mark.parametrize("args", [["--help"], []])
def test_help(monkeypatch, args):
    monkeypatch.delenv("FORGE_BASE_URL", raising=False)

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "--base-url" in result.output
    assert "--version" in result.output


@pytest.mark.parametrize("args", [["--help"], ["--version"]])
def test_eager_options_bypass_invalid_config(monkeypatch, args):
    monkeypatch.setenv("FORGE_BASE_URL", "invalid")

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    if args == ["--version"]:
        assert result.output.strip() == "0.1.0"
    else:
        assert "Usage:" in result.output


@pytest.mark.parametrize("args", [["--unknown"], ["unknown"]])
def test_unknown_input(args):
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert "unknown" in result.output


def test_base_url_flag_overrides_invalid_environment(monkeypatch):
    monkeypatch.setenv("FORGE_BASE_URL", "invalid")

    result = runner.invoke(app, ["--base-url", "http://127.0.0.1:1"])

    assert result.exit_code == 0, result.output


def test_invalid_environment_url_does_not_echo_credentials(monkeypatch):
    monkeypatch.setenv("FORGE_BASE_URL", "https://user:example-secret@forge.example.invalid")

    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "FORGE_BASE_URL" in result.output
    assert "example-secret" not in result.output
