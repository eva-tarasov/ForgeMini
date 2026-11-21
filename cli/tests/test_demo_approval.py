"""Harness safeguards only. Never connect to Docker, PostgreSQL or TCP here."""
import asyncio
import json
import os
import socket
from types import SimpleNamespace

import httpx
import pytest

import demo_approval as demo


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('External execution is forbidden in guard tests')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(demo.logging, 'disable', lambda _: None)
    monkeypatch.setattr(demo.subprocess, 'run', deny)
    monkeypatch.setattr(demo.os, 'execve', deny)


@pytest.mark.parametrize('args', [['tcp'], ['interactive'], ['tcp', '--approve-db', 'forge', '--confirm-fixture-write'],
    ['tcp', '--approve-db', demo.DB], ['live'], ['tcp', '--confirm-fixture-write']])
def test_modes_require_explicit_guard(args):
    with pytest.raises(SystemExit) as caught:
        demo.main(['launch', *args])
    assert caught.value.code == 2


@pytest.mark.parametrize('prefix', [[], ['launch']])
def test_describe_is_completely_offline(prefix, capsys):
    assert demo.main([*prefix, 'describe']) == 0
    body = json.loads(capsys.readouterr().out)
    assert body['database'] == demo.DB and body['provider'] == 'offline MockTransport'
    assert body['scenario_seconds'] == 240


def test_help_never_inspects_docker():
    with pytest.raises(SystemExit) as caught:
        demo.main(['launch', '--help'])
    assert caught.value.code == 0


def test_direct_execution_requires_wrapper(monkeypatch, capsys):
    monkeypatch.delenv('FORGE_APPROVAL_CONTAINER_VERIFIED', raising=False)
    def immediate(coroutine):
        return coroutine.send(None)
    monkeypatch.setattr(demo.asyncio, 'run', immediate)
    assert demo.main(['tcp', '--approve-db', demo.DB, '--confirm-fixture-write']) == 1
    body = json.loads(capsys.readouterr().out)
    assert body['cleanup'] == 'not_started' and body['failure'] == 'use_reviewed_owner_wrapper'


def container():
    return dict(Name='/' + demo.CONTAINER, State={'Running': True}, Config={'Env': [
        'POSTGRES_DB=' + demo.DB, 'POSTGRES_USER=fixture', 'POSTGRES_PASSWORD=synthetic-password']},
        NetworkSettings={'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '50088'}]}})


@pytest.mark.parametrize('fault', ['none', 'name', 'stopped', 'port', 'database', 'revision'])
def test_launcher_identity_and_environment(monkeypatch, fault):
    body = container()
    if fault == 'name':
        body['Name'] = '/other'
    elif fault == 'stopped':
        body['State']['Running'] = False
    elif fault == 'port':
        body['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'] = '5432'
    calls = []
    def docker(*args):
        calls.append(args)
        if args[0] == 'inspect':
            return json.dumps([body]).encode()
        database = 'postgres' if fault == 'database' else demo.DB
        return (database + '|170011|' + ('wrong' if fault == 'revision' else '1d972cc34fbe')).encode()
    monkeypatch.setattr(demo, 'docker_output', docker)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-unwanted-key')
    monkeypatch.setenv('LANGSMITH_API_KEY', 'synthetic-unwanted-key')
    if fault != 'none':
        with pytest.raises(demo.shared.DemoError):
            demo.launcher_environment()
    else:
        env = demo.launcher_environment()
        assert env['POSTGRES_DB'] == demo.DB and env['LANGSMITH_TRACING'] == 'false'
        assert 'OPENAI_API_KEY' not in env and 'LANGSMITH_API_KEY' not in env
        assert env['FORGE_APPROVAL_CONTAINER_VERIFIED'] == demo.CONTAINER
        assert len(calls) == 2


@pytest.mark.parametrize('bootstrap_database', ['postgres', None], ids=['different-bootstrap', 'no-bootstrap'])
def test_launcher_checks_actual_fixture_database(monkeypatch, bootstrap_database):
    body = container()
    body['Config']['Env'] = [item for item in body['Config']['Env'] if not item.startswith('POSTGRES_DB=')]
    if bootstrap_database is not None:
        body['Config']['Env'].append('POSTGRES_DB=' + bootstrap_database)
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == 'inspect':
            return json.dumps([body]).encode()
        assert args[args.index('-d') + 1] == demo.DB
        return (demo.DB + '|170011|1d972cc34fbe').encode()

    monkeypatch.setattr(demo, 'docker_output', docker)
    env = demo.launcher_environment()

    assert env['POSTGRES_DB'] == demo.DB
    assert env['FORGE_REPOSITORY_AGENT_TEST_DB'] == demo.DB
    assert len(calls) == 2


def test_docker_errors_do_not_echo_private_output(monkeypatch):
    monkeypatch.setattr(demo.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=b'private', stderr=b'private'))
    with pytest.raises(demo.shared.DemoError, match='docker_preflight_failed'):
        demo.docker_output('inspect', demo.CONTAINER)


def test_driver_environment_excludes_provider_and_db(monkeypatch):
    monkeypatch.setenv('POSTGRES_PASSWORD', 'synthetic-password')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')
    driver = demo.Driver(httpx)
    env = driver.environment()
    assert 'POSTGRES_PASSWORD' not in env and 'OPENAI_API_KEY' not in env
    assert 'FORGE_APPROVAL_READ_TOKEN' in env and 'FORGE_APPROVAL_REVIEW_TOKEN' in env


def test_scope_guard_allows_only_exact_fixture(tmp_path):
    (tmp_path / 'staged').write_bytes(b'after')
    (tmp_path / demo.FILE).write_bytes(b'before')
    count = []
    replace = demo.scoped_replacer(tmp_path, count, os.replace)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(demo.shared.DemoError, match='unscoped_filesystem_replace'):
            replace('staged', demo.FILE)
        with pytest.raises(demo.shared.DemoError, match='filesystem_replace_outside_scope'):
            replace('staged', 'outside.txt', src_dir_fd=fd, dst_dir_fd=fd)
        replace('staged', demo.FILE, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
    assert count == [demo.FILE] and (tmp_path / demo.FILE).read_bytes() == b'after'


def test_offline_provider_request_cap(monkeypatch):
    # The reply coroutine has no suspension points or network operations.
    async def check():
        provider = demo.OfflineProvider()
        for _ in range(4):
            request = httpx.Request('POST', 'https://approval.invalid/v1/chat/completions', json={
                'model': 'gpt-4o-mini', 'max_completion_tokens': 512,
                'response_format': {'type': 'json_schema'}})
            response = await provider.reply(request)
            assert response.status_code == 200
        with pytest.raises(demo.shared.DemoError, match='offline_model_budget_exceeded'):
            await provider.reply(request)
    # Running the coroutine directly is sufficient because it never suspends.
    coroutine = check()
    with pytest.raises(StopIteration):
        coroutine.send(None)


def test_terminal_refuses_override_before_session_access():
    with pytest.raises(demo.shared.DemoError, match='fixed_backend_required'):
        demo.terminal(['--session', '/tmp/forge-approval-test/capability.sock', 'approval', '--base-url=https://outside.invalid'])


def test_settlement_deadline_does_not_cancel_backend_task(monkeypatch):
    monkeypatch.setattr(demo, 'SYNC_SECONDS', 0)
    async def scenario():
        task = asyncio.create_task(asyncio.Event().wait())
        try:
            with pytest.raises(TimeoutError):
                await demo.settled(None, task)
            assert not task.done() and not task.cancelling()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    # Permit only local AF_UNIX sockets needed by asyncio; block IP sockets and DNS.
    loop_socket = __import__('socket').SocketType
    def local_socket(family=-1, type=-1, proto=-1, fileno=None):
        if family != socket.AF_UNIX:
            pytest.fail('IP sockets forbidden')
        return loop_socket(family, type, proto, fileno=fileno)
    monkeypatch.setattr(socket, 'socket', local_socket)
    asyncio.run(scenario())
