"""Release guard tests remain offline. The clean-install launcher is explicitly opt-in."""
import json
import os
import socket
from zipfile import ZipFile

import pytest

import clean_install as release


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('Release unit tests must not install packages or open sockets')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(release.subprocess, 'Popen', deny)


def test_cli_environment_drops_configuration_and_secrets(monkeypatch):
    for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'FORGE_BASE_URL', 'FORGE_SYNTHETIC_TOKEN',
                'OPENAI_API_KEY', 'POSTGRES_PASSWORD', 'PIP_INDEX_URL'):
        monkeypatch.setenv(key, 'synthetic-sensitive-value')
    assert not set(('PYTHONPATH', 'VIRTUAL_ENV', 'FORGE_BASE_URL', 'FORGE_SYNTHETIC_TOKEN',
                    'OPENAI_API_KEY', 'POSTGRES_PASSWORD', 'PIP_INDEX_URL')).intersection(release.environment())
    assert release.environment()['PIP_CONFIG_FILE'] == os.devnull
    installer = release.environment(installer=True)
    assert 'PIP_INDEX_URL' in installer and 'PYTHONPATH' not in installer and 'OPENAI_API_KEY' not in installer


@pytest.mark.parametrize('unexpected', ['app/main.py', 'tests/test_fake.py'])
def test_wheel_rejects_backend_and_test_payloads(tmp_path, unexpected):
    wheel = tmp_path / 'release.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('forge_cli/main.py', '')
        archive.writestr(unexpected, '')
    with pytest.raises(release.SmokeFailure, match='unexpected_wheel_payload'):
        release.inspect_wheel(wheel)


def test_valid_wheel_layout(tmp_path):
    wheel = tmp_path / 'release.whl'
    with ZipFile(wheel, 'w') as archive:
        archive.writestr('forge_cli/main.py', '')
        archive.writestr('forge_cli-0.1.0.dist-info/METADATA', 'Version: 0.1.0')
    release.inspect_wheel(wheel)


def test_failed_build_cleans_only_disposable_environment(monkeypatch):
    observed = []
    def fail(stage, command, cwd, **kwargs):
        assert stage == 'venv'
        observed.append(cwd.parent)
        assert cwd.parent.name.startswith('forge-cli-release-')
        assert not cwd.is_relative_to(release.ROOT)
        raise release.SmokeFailure(stage, 'synthetic_failure')
    monkeypatch.setattr(release, 'run_stage', fail)
    report = {}
    with pytest.raises(release.SmokeFailure):
        release.smoke(report)
    assert report['cleanup'] == 'completed'
    assert observed and not observed[0].exists()
    assert (release.ROOT / 'cli/pyproject.toml').is_file()


def test_unavailable_index_is_reported_blocked(monkeypatch, capsys):
    def blocked(report):
        report['cleanup'] = 'completed'
        raise release.SmokeFailure('install', 'unexpected_exit', blocked=True)
    monkeypatch.setattr(release, 'smoke', blocked)
    assert release.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['outcome'] == 'BLOCKED' and report['stage'] == 'install'


def test_help_does_not_install():
    with pytest.raises(SystemExit) as caught:
        release.main(['--help'])
    assert caught.value.code == 0
