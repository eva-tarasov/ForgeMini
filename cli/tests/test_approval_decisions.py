"""Offline decision contracts, with synthetic capabilities and blocked real sockets."""
import json
import secrets
import socket
from contextlib import redirect_stdout
from io import StringIO
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from forge_cli import commands, output
from forge_cli.client import ForgeAPIClient
from forge_cli.dto import ApprovalDecisionResponse
from forge_cli.main import app

RUN = UUID('11111111-1111-4111-8111-111111111111')
APPROVAL = UUID('22222222-2222-4222-8222-222222222222')
OTHER = UUID('33333333-3333-4333-8333-333333333333')
DIGEST = '8b' * 32


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('Real sockets are forbidden')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.delenv('FORGE_BASE_URL', raising=False)
    monkeypatch.setenv('FORGE_APPROVAL_READ_TOKEN', secrets.token_urlsafe(36))
    monkeypatch.setenv('FORGE_APPROVAL_REVIEW_TOKEN', secrets.token_urlsafe(36))


@pytest.fixture
def api(monkeypatch):
    state = dict(status=200, body=None, raw=None, calls=[], timeout=False)
    def handle(request):
        import os
        if request.url.path == '/health':
            if 'authorization' in request.headers:
                pytest.fail('Credentials reached an unrelated endpoint')
            return httpx.Response(200, json={'status': 'ok'})
        if request.headers.get('authorization') != 'Bearer ' + os.environ['FORGE_APPROVAL_REVIEW_TOKEN']:
            pytest.fail('Incorrect capability selection')
        body = json.loads(request.content)
        state['calls'].append((request.method, request.url.path, body))
        if state['timeout']:
            raise httpx.ReadTimeout('private transport content', request=request)
        response = state['body']
        if response is None:
            response = dict(run_id=str(RUN), approval_id=str(APPROVAL),
                            decision='approved' if body['decision'] == 'approve' else 'rejected', recorded=True)
        if state['raw'] is not None:
            return httpx.Response(state['status'], content=state['raw'])
        return httpx.Response(state['status'], json=response)
    def client(base_url='http://localhost'):
        return ForgeAPIClient(base_url, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(commands, 'ForgeAPIClient', client)
    state['client'] = client
    return state


def args(decision):
    command = ['approval', decision, str(RUN), str(APPROVAL)]
    if decision == 'approve':
        command += ['--reviewed-patch-sha256', DIGEST]
    return command + ['--confirm']


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_exact_post_and_json(api, decision):
    result = CliRunner().invoke(app, ['--json', *args(decision)])
    assert result.exit_code == 0 and result.stderr == ''
    body = {'decision': decision}
    if decision == 'approve':
        body['reviewed_patch_sha256'] = DIGEST
    assert api['calls'] == [('POST', f'/agent-runs/{RUN}/approvals/{APPROVAL}/decision', body)]
    assert json.loads(result.stdout)['recorded'] is True
    with api['client']() as client:
        client.get_health()


@pytest.mark.parametrize('command', [args('approve')[:-1], args('reject')[:-1],
    ['approval', 'approve', str(RUN), str(APPROVAL), '--confirm'],
    ['approval', 'reject', 'invalid', str(APPROVAL), '--confirm'],
    ['approval', 'reject', str(RUN), 'invalid', '--confirm'],
    *[['approval', 'approve', str(RUN), str(APPROVAL), '--confirm', '--reviewed-patch-sha256', value]
      for value in ['bad', 'A' * 64, '0' * 63, '0' * 65, '\x1b' * 64]]])
def test_invalid_input_makes_no_http(api, command):
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2 and result.stdout == ''
    assert not api['calls']


@pytest.mark.parametrize('decision', ['approve', 'reject'])
@pytest.mark.parametrize('status', [403, 404, 409, 422, 500, 503])
def test_http_failures(api, decision, status):
    api.update(status=status, body={'error': {'code': 'private-patch', 'message': 'private-patch'}})
    result = CliRunner().invoke(app, args(decision))
    assert result.exit_code == 4 and result.stdout == ''
    assert 'private-patch' not in result.stderr
    assert len(api['calls']) == 1


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_timeout_is_unknown_and_never_retried(api, decision):
    api['timeout'] = True
    result = CliRunner().invoke(app, args(decision))
    assert result.exit_code == 3 and result.stdout == ''
    assert 'outcome is unknown' in result.stderr and 'forge approval get' in result.stderr
    assert 'private transport content' not in result.stderr
    assert len(api['calls']) == 1


@pytest.mark.parametrize('change', [dict(run_id=str(OTHER)), dict(approval_id=str(OTHER)), dict(decision='rejected'),
    dict(recorded=1), dict(recorded=False), dict(recorded='true'), dict(extra=True)])
def test_malformed_success(api, change):
    api['body'] = dict(run_id=str(RUN), approval_id=str(APPROVAL), decision='approved', recorded=True, **{})
    api['body'].update(change)
    result = CliRunner().invoke(app, args('approve'))
    assert result.exit_code == 5 and result.stdout == '' and len(api['calls']) == 1


def test_malformed_json(api):
    api['raw'] = b'{'
    assert CliRunner().invoke(app, args('reject')).exit_code == 5
    assert len(api['calls']) == 1


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_missing_review_does_not_use_read(api, monkeypatch, decision):
    monkeypatch.delenv('FORGE_APPROVAL_REVIEW_TOKEN')
    assert CliRunner().invoke(app, args(decision)).exit_code == 2
    assert not api['calls']


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_help_without_tokens(api, monkeypatch, decision):
    monkeypatch.delenv('FORGE_APPROVAL_REVIEW_TOKEN')
    monkeypatch.delenv('FORGE_APPROVAL_READ_TOKEN')
    assert CliRunner().invoke(app, ['approval', decision, '--help']).exit_code == 0
    assert not api['calls']


@pytest.mark.parametrize('width', [32, 80])
@pytest.mark.parametrize('tty', [True, False])
@pytest.mark.parametrize('decision', ['approved', 'rejected'])
def test_safe_human_output(width, tty, decision, monkeypatch):
    class Stream(StringIO):
        def isatty(self):
            return tty
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('COLUMNS', str(width))
    model = ApprovalDecisionResponse(run_id=RUN, approval_id=APPROVAL, decision=decision, recorded=True)
    stream = Stream()
    with redirect_stdout(stream):
        output.print_result(model, json_output=False, no_color=True)
    rendered = stream.getvalue()
    assert str(RUN) in rendered and str(APPROVAL) in rendered and decision in rendered
    assert '\x1b' not in rendered and 'succeeded' not in rendered


def test_malicious_response_never_reaches_terminal(api):
    api['body'] = dict(run_id=str(RUN), approval_id=str(APPROVAL), decision='\x1b[2J[bold]', recorded=True)
    result = CliRunner().invoke(app, args('reject'))
    assert result.exit_code == 5 and '\x1b' not in result.output and '[bold]' not in result.output


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_review_capability_works_without_read(api, monkeypatch, decision):
    monkeypatch.delenv('FORGE_APPROVAL_READ_TOKEN')
    result = CliRunner().invoke(app, args(decision))
    assert result.exit_code == 0 and len(api['calls']) == 1


@pytest.mark.parametrize('decision', ['approve', 'reject'])
def test_plaintext_nonloopback_is_rejected_locally(api, monkeypatch, decision):
    monkeypatch.setenv('FORGE_BASE_URL', 'http://outside.invalid')
    result = CliRunner().invoke(app, args(decision))
    assert result.exit_code == 2 and not api['calls']


def test_backend_cannot_echo_capability_into_diagnostics(api):
    import os
    secret = os.environ['FORGE_APPROVAL_REVIEW_TOKEN']
    api.update(status=500, body={'error': {'code': secret, 'message': secret}})
    result = CliRunner().invoke(app, args('reject'))
    if secret in result.output or secret in repr(result.exception):
        pytest.fail('Capability leaked into diagnostics')
    assert result.exit_code == 4
