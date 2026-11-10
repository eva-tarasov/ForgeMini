import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import ROOT_ENV, Settings
from app.db.session import create_engine, create_session_factory


@pytest.fixture(autouse=True)
def clear_postgres_environment(monkeypatch):
    for name in (
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def valid_settings(**overrides):
    values = {
        "postgres_user": "test_user",
        "postgres_password": "test_password",
        "postgres_db": "test_db",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_root_env_path_and_loading_from_backend_directory(tmp_path, monkeypatch):
    assert ROOT_ENV == Path(__file__).resolve().parents[2] / ".env"
    assert Settings.model_config["env_file"] == ROOT_ENV
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_USER=dotenv_user\nPOSTGRES_PASSWORD='p@ss:/?#%+ with spaces'\n"
        "POSTGRES_DB=dotenv_db\nPOSTGRES_PORT=55433\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    monkeypatch.chdir(backend_dir)

    settings = Settings()
    assert settings.postgres_user == "dotenv_user"
    assert settings.postgres_password.get_secret_value() == "p@ss:/?#%+ with spaces"
    assert settings.postgres_db == "dotenv_db"
    assert settings.postgres_port == 55433


@pytest.mark.parametrize(
    "overrides",
    [
        {"postgres_user": " "},
        {"postgres_password": " "},
        {"postgres_db": ""},
        {"postgres_host": " "},
        {"postgres_port": 0},
        {"postgres_port": 65536},
        {"postgres_port": "invalid"},
    ],
)
def test_invalid_settings_fail_before_engine_creation(overrides):
    with pytest.raises(ValidationError):
        valid_settings(**overrides)


def test_missing_required_settings_fail_before_engine_creation():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_password_is_hidden_in_settings_and_validation_errors():
    fake_password = "synthetic-secret"
    settings = valid_settings(postgres_password=fake_password)
    assert fake_password not in repr(settings)
    with pytest.raises(ValidationError) as exc_info:
        valid_settings(postgres_password=123456789)
    assert "123456789" not in str(exc_info.value)


def test_url_handles_special_password_and_factory_makes_independent_sessions():
    fake_password = "p@ss:/?#%+ ${literal} with spaces"
    settings = valid_settings(postgres_password=fake_password)
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    async def check_and_dispose():
        try:
            assert engine.url.drivername == "postgresql+asyncpg"
            assert engine.url.password == fake_password
            assert fake_password not in str(engine.url)
            async with factory() as first, factory() as second:
                assert first is not second
                assert first.sync_session.expire_on_commit is False
        finally:
            await engine.dispose()

    asyncio.run(check_and_dispose())
