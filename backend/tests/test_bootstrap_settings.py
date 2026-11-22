"""Settings coming from a user's .env: the values must actually take effect."""

import pytest

from app.bootstrap import ForgeMiniSettings, create_forge_app
from app.core.settings import Settings
from app.sandbox.docker_sandbox import default_image


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # create_forge_app() writes os.environ["FORGE_SANDBOX_IMAGE"]. delenv() on an absent key records nothing,
    # so set-then-delete first: that makes monkeypatch restore the true original state on teardown.
    monkeypatch.setenv("FORGE_SANDBOX_IMAGE", "placeholder")
    for key in ("FORGE_SANDBOX_IMAGE", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DB", "OPENAI_API_KEY", "FORGE_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)


def test_sandbox_image_from_dotenv_reaches_default_image(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FORGE_SANDBOX_IMAGE=my/custom:1\n")
    settings = ForgeMiniSettings(_env_file=env, forge_data_dir=tmp_path / "d")
    assert settings.forge_sandbox_image == "my/custom:1"
    create_forge_app(settings, session_factory=object())  # wiring only; no DB access at construction
    assert default_image() == "my/custom:1"


def test_default_image_unchanged_when_not_configured(tmp_path, monkeypatch):
    create_forge_app(ForgeMiniSettings(_env_file=None, forge_data_dir=tmp_path / "d"), session_factory=object())
    assert default_image() == "python:3.12-slim"


def test_db_settings_work_from_the_documented_env_example(tmp_path):
    """The README flow: copy .env.example, set a password. POSTGRES_HOST is NOT in that file."""
    env = tmp_path / ".env"
    env.write_text("POSTGRES_USER=forge\nPOSTGRES_PASSWORD=s3cret\nPOSTGRES_DB=forge\nPOSTGRES_PORT=55432\n")
    cfg = Settings(_env_file=env)
    assert cfg.postgres_host == "127.0.0.1" and cfg.postgres_port == 55432


def test_empty_exported_host_is_rejected_not_silently_defaulted(tmp_path, monkeypatch):
    """Documents WHY start.sh must never assign POSTGRES_HOST: an empty exported value is a hard error."""
    monkeypatch.setenv("POSTGRES_HOST", "")
    env = tmp_path / ".env"
    env.write_text("POSTGRES_USER=forge\nPOSTGRES_PASSWORD=s3cret\nPOSTGRES_DB=forge\n")
    with pytest.raises(Exception):
        Settings(_env_file=env)
