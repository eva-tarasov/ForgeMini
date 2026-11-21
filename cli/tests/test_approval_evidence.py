"""Offline capability reads. All credentials are synthetic and stay out of diagnostics."""

import json
import secrets
import socket
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from forge_cli import commands, output
from forge_cli.client import ForgeAPIClient
from forge_cli.config import CapabilityConfigurationError
from forge_cli.dto import ApprovalPatchReview, EvidenceAttemptDetail
from forge_cli.errors import ForgeAPIError, ForgeResponseError
from forge_cli.main import app

RUN = UUID('11111111-1111-4111-8111-111111111111')
APPROVAL = UUID('22222222-2222-4222-8222-222222222222')
ATTEMPT = UUID('33333333-3333-4333-8333-333333333333')
OTHER = UUID('44444444-4444-4444-8444-444444444444')
STAMP = '2026-11-21T12:00:00+00:00'
PATCH = '--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-old\n+Привет [bold]  \n'
BASE = f'/agent-runs/{RUN}'
PENDING = dict(id=str(APPROVAL), run_id=str(RUN), tool_name='apply_patch', status='pending', requested_at=STAMP)
PREVIEW = dict(state='live_at_read', content_included=False,
               files=[dict(path='sample.txt', added_lines=1, removed_lines=1, hunk_count=1)])
DETAIL = dict(**PENDING, decided_at=None, preview=PREVIEW)
SUMMARY = dict(attempt_id=str(ATTEMPT), sequence=3, verdict='pass', file_verdict='pass',
               audit_complete=True, isolated_fixture=True, check_count=1)
CHECK = dict(check_id=str(OTHER), criterion_index=0, path='sample.txt', outcome='match')
EVIDENCE = dict(**SUMMARY, checks=[CHECK])
PAGE = dict(run_id=str(RUN), items=[SUMMARY], next_after_sequence=3, has_more=False)


def review(patch=PATCH):
    return dict(approval_id=str(APPROVAL), run_id=str(RUN), patch=patch,
                patch_sha256=sha256(b'forge:apply_patch:v1\0' + patch.encode()).hexdigest())


CASES = [
    ('list_approvals', (RUN,), ['approval', 'list', str(RUN)], '/approvals', {'items': [PENDING]}),
    ('get_approval', (RUN, APPROVAL), ['approval', 'get', str(RUN), str(APPROVAL)], f'/approvals/{APPROVAL}', DETAIL),
    ('get_approval_review', (RUN, APPROVAL), ['approval', 'review', str(RUN), str(APPROVAL)],
     f'/approvals/{APPROVAL}/review', review()),
    ('list_evidence', (RUN,), ['evidence', 'list', str(RUN)], '/evidence', PAGE),
    ('get_evidence', (RUN, ATTEMPT), ['evidence', 'get', str(RUN), str(ATTEMPT)], f'/evidence/{ATTEMPT}', EVIDENCE),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('Real network is forbidden')
    monkeypatch.setattr(socket, 'socket', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.delenv('FORGE_BASE_URL', raising=False)
    monkeypatch.setenv('FORGE_APPROVAL_READ_TOKEN', secrets.token_urlsafe(36))
    monkeypatch.setenv('FORGE_APPROVAL_REVIEW_TOKEN', secrets.token_urlsafe(36))


@pytest.fixture
def api(monkeypatch):
    state = dict(body={}, status=200, requests=[], raw=None)

    def handle(request):
        state['requests'].append((request.method, request.url.path, dict(request.url.params)))
        variable = 'FORGE_APPROVAL_REVIEW_TOKEN' if request.url.path.endswith('/review') else 'FORGE_APPROVAL_READ_TOKEN'
        import os
        if request.url.path == '/health':
            if 'authorization' in request.headers:
                pytest.fail('Unrelated request received credentials')
        elif request.headers.get('Authorization') != 'Bearer ' + os.environ.get(variable, ''):
            pytest.fail('Wrong capability header')
        return httpx.Response(state['status'], content=state['raw']) if state['raw'] is not None else httpx.Response(
            state['status'], json=state['body'])

    def client(url='http://localhost:8000'):
        return ForgeAPIClient(url, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(commands, 'ForgeAPIClient', client)
    state['client'] = client
    return state


@pytest.mark.parametrize('method,ids,args,suffix,body', CASES)
def test_five_commands_and_typed_methods(api, method, ids, args, suffix, body):
    api['body'] = body
    with api['client']() as client:
        typed = getattr(client, method)(*ids)
    result = CliRunner().invoke(app, ['--json', *args])
    assert result.exit_code == 0 and result.stderr == ''
    assert json.loads(result.stdout) == typed.model_dump(mode='json')
    expected_params = {'after_sequence': '0', 'limit': '20'} if method == 'list_evidence' else {}
    assert api['requests'] == [('GET', BASE + suffix, expected_params)] * 2


@pytest.mark.parametrize('method,body', [('list_approvals', {'items': []}),
    ('list_evidence', dict(run_id=str(RUN), items=[], next_after_sequence=0, has_more=False))])
def test_empty_pages(api, method, body):
    api['body'] = body
    with api['client']() as client:
        assert getattr(client, method)(RUN).items == []


@pytest.mark.parametrize('status,decided', [('pending', None), ('approved', STAMP), ('rejected', STAMP)])
@pytest.mark.parametrize('preview', [PREVIEW, dict(state='unavailable', content_included=False, files=[])])
def test_approval_states(api, status, decided, preview):
    api['body'] = dict(DETAIL, status=status, decided_at=decided, preview=preview)
    with api['client']() as client:
        assert client.get_approval(RUN, APPROVAL).status == status


@pytest.mark.parametrize('method,ids,args,suffix,body', CASES)
@pytest.mark.parametrize('status', [403, 404, 409, 422, 500, 503, 302])
def test_safe_http_errors(api, method, ids, args, suffix, body, status):
    api['status'] = status
    api['body'] = {'error': {'code': 'private-value', 'message': PATCH}}
    with api['client']() as client, pytest.raises(ForgeAPIError) as caught:
        getattr(client, method)(*ids)
    assert caught.value.status_code == status
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 4 and result.stdout == ''
    assert 'private-value' not in result.stderr and PATCH not in result.stderr


@pytest.mark.parametrize('method,ids,args,suffix,body', CASES)
@pytest.mark.parametrize('corruption', ['identity', 'extra', 'json'])
def test_invalid_success_is_exit_five(api, method, ids, args, suffix, body, corruption):
    body = deepcopy(body)
    if corruption == 'extra':
        body['unexpected'] = True
    elif corruption == 'json':
        api['raw'] = b'{'
    elif method == 'list_approvals':
        body['items'][0]['run_id'] = str(OTHER)
    else:
        body['attempt_id' if method == 'get_evidence' else 'run_id'] = str(OTHER)
    api['body'] = body
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 5 and result.stdout == ''
    assert PATCH not in result.stderr


@pytest.mark.parametrize('token', ['', 'short', 'a' * 513, 'a' * 32 + '\n', 'a' * 32 + ' ', 'a' * 32 + '=',
                                 'a' * 32 + ':', 'é' * 32],
                         ids=['missing', 'short', 'oversized', 'newline', 'space', 'padded', 'punctuation', 'unicode'])
def test_token_format(api, monkeypatch, token):
    monkeypatch.setenv('FORGE_APPROVAL_READ_TOKEN', token)
    api['body'] = {'items': []}
    valid = token == 'a' * 32 + '='
    with api['client']() as client:
        if valid:
            client.list_approvals(RUN)
        else:
            with pytest.raises(CapabilityConfigurationError):
                client.list_approvals(RUN)
    assert len(api['requests']) == int(valid)


@pytest.mark.parametrize('host,allowed', [('localhost', True), ('127.5.6.7', True), ('[::1]', True),
    ('localhost.evil', False), ('localhost.', False), ('127.0.0.1.evil', False), ('2130706433', False),
    ('example.com', False), ('192.168.1.1', False), ('[::ffff:127.0.0.1]', False)])
def test_http_destinations(api, host, allowed):
    api['body'] = {'items': []}
    with api['client']('http://' + host) as client:
        if allowed:
            client.list_approvals(RUN)
        else:
            with pytest.raises(CapabilityConfigurationError):
                client.list_approvals(RUN)
    assert len(api['requests']) == int(allowed)


def test_capabilities_are_separate_and_request_scoped(api, monkeypatch):
    monkeypatch.delenv('FORGE_APPROVAL_READ_TOKEN')
    api['body'] = review()
    with api['client']('https://forge.example') as client:
        with pytest.raises(CapabilityConfigurationError):
            client.list_approvals(RUN)
        client.get_approval_review(RUN, APPROVAL)
        monkeypatch.delenv('FORGE_APPROVAL_REVIEW_TOKEN')
        with pytest.raises(CapabilityConfigurationError):
            client.get_approval_review(RUN, APPROVAL)
        api['body'] = {'status': 'ok'}
        client.get_health()
    assert len(api['requests']) == 2


@pytest.mark.parametrize('args', [case[2][:-1] + ['invalid'] for case in CASES] + [
    ['evidence', 'list', str(RUN), '--limit', '0'], ['evidence', 'list', str(RUN), '--limit', '21'],
    ['evidence', 'list', str(RUN), '--after-sequence', '-1'],
    ['evidence', 'list', str(RUN), '--after-sequence', '2147483648']])
def test_local_arguments(api, args):
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    assert api['requests'] == []


@pytest.mark.parametrize('args', [['--help'], ['--version'], ['approval', '--help'],
    ['evidence', '--help'], *[case[2][:2] + ['--help'] for case in CASES]])
def test_help_needs_no_credentials(api, monkeypatch, args):
    monkeypatch.delenv('FORGE_APPROVAL_READ_TOKEN')
    monkeypatch.delenv('FORGE_APPROVAL_REVIEW_TOKEN')
    assert CliRunner().invoke(app, args).exit_code == 0
    assert api['requests'] == []


@pytest.mark.parametrize('sequences,after,limit,cursor,more,valid', [
    ([3, 8], 1, 2, 8, True, True), ([3, 8], 1, 2, 8, False, True),
    ([], 5, 2, 5, False, True), ([], 5, 2, 0, False, False), ([], 0, 2, 0, True, False),
    ([3], 0, 2, 3, True, False), ([3, 3], 0, 2, 3, False, False),
    ([8, 3], 0, 2, 3, False, False), ([3], 3, 2, 3, False, False),
    ([3, 8], 0, 1, 8, False, False), ([3], 0, 1, 4, False, False),
    ([2147483647], 0, 1, 2147483647, True, False),
])
def test_evidence_cursor_semantics(api, sequences, after, limit, cursor, more, valid):
    api['body'] = dict(PAGE, items=[dict(SUMMARY, sequence=n) for n in sequences],
                       next_after_sequence=cursor, has_more=more)
    with api['client']() as client:
        if valid:
            client.list_evidence(RUN, after_sequence=after, limit=limit)
        else:
            with pytest.raises(ForgeResponseError):
                client.list_evidence(RUN, after_sequence=after, limit=limit)
    assert api['requests'][0][2] == {'after_sequence': str(after), 'limit': str(limit)}


@pytest.mark.parametrize('change', ['fingerprint', 'truncated', 'oversized', 'unicode_bytes', 'uppercase', 'surrogate'])
def test_patch_integrity(api, change):
    body = review()
    if change == 'fingerprint':
        body['patch_sha256'] = sha256(PATCH.encode()).hexdigest()
    elif change == 'truncated':
        body['patch'] = PATCH[:-1]
    elif change in ('oversized', 'unicode_bytes'):
        body = review(('x' * 65537) if change == 'oversized' else 'я' * 32769)
    elif change == 'uppercase':
        body['patch_sha256'] = body['patch_sha256'].upper()
    else:
        body['patch'] = '\ud800'
    api['raw'] = json.dumps(body).encode()
    with api['client']() as client, pytest.raises(ForgeResponseError):
        client.get_approval_review(RUN, APPROVAL)


@pytest.mark.parametrize('verdict', ['pass', 'fail', 'inconclusive'])
@pytest.mark.parametrize('outcome', ['match', 'mismatch', 'read_error', 'unknown_effect', 'ambiguous', 'missing'])
def test_evidence_verdicts_and_outcomes(verdict, outcome):
    model = EvidenceAttemptDetail.model_validate_json(json.dumps(dict(EVIDENCE, verdict=verdict,
        file_verdict=verdict, checks=[dict(CHECK, outcome=outcome)])))
    assert model.verdict == verdict and model.checks[0].outcome == outcome


@pytest.mark.parametrize('field,value', [
    ('status', 'unknown'), ('status', 'approved'), ('decided_at', STAMP), ('requested_at', '2026-11-21T12:00:00'),
    ('requested_at', 1), ('id', 'bad'), ('tool_name', '[bold]'),
    ('preview', dict(PREVIEW, content_included=0)), ('preview', dict(PREVIEW, content_included=1)),
    ('preview', dict(PREVIEW, content_included=True)), ('preview', dict(PREVIEW, files=[])),
    ('preview', dict(PREVIEW, files=PREVIEW['files'] * 5)), ('preview', dict(PREVIEW, state='unknown')),
    ('preview', dict(state='unavailable', content_included=False, files=PREVIEW['files'])),
    ('preview', dict(PREVIEW, files=[dict(PREVIEW['files'][0], added_lines=1001)])),
    ('preview', dict(PREVIEW, files=[dict(PREVIEW['files'][0], removed_lines=-1)])),
    ('preview', dict(PREVIEW, files=[dict(PREVIEW['files'][0], hunk_count=0)])),
    ('preview', dict(PREVIEW, files=[dict(PREVIEW['files'][0], hunk_count=33)])),
    ('preview', dict(PREVIEW, files=[dict(PREVIEW['files'][0], added_lines=True)])),
])
def test_invalid_approval_metadata(api, field, value):
    api['body'] = dict(DETAIL, **{field: value})
    with api['client']() as client, pytest.raises(ForgeResponseError):
        client.get_approval(RUN, APPROVAL)


@pytest.mark.parametrize('field,value', [
    ('audit_complete', 0), ('isolated_fixture', 1), ('check_count', True), ('check_count', 0), ('check_count', 33),
    ('check_count', 2), ('sequence', 0), ('sequence', 2147483648), ('sequence', '3'),
    ('verdict', 'success'), ('file_verdict', 'unknown'), ('checks', []), ('checks', [CHECK] * 33),
    ('checks', [dict(CHECK, criterion_index=10)]), ('checks', [dict(CHECK, criterion_index=-1)]),
    ('checks', [dict(CHECK, outcome='success')]), ('checks', [dict(CHECK, extra=True)]),
])
def test_invalid_evidence_metadata(api, field, value):
    api['body'] = dict(EVIDENCE, **{field: value})
    with api['client']() as client, pytest.raises(ForgeResponseError):
        client.get_evidence(RUN, ATTEMPT)


@pytest.mark.parametrize('field,value', [('has_more', 0), ('has_more', 1), ('items', [SUMMARY] * 21)])
def test_strict_evidence_page(api, field, value):
    api['body'] = dict(PAGE, **{field: value})
    with api['client']() as client, pytest.raises(ForgeResponseError):
        client.list_evidence(RUN)


def test_pending_approval_limit(api):
    api['body'] = {'items': [PENDING] * 33}
    with api['client']() as client, pytest.raises(ForgeResponseError):
        client.list_approvals(RUN)


@pytest.mark.parametrize('kwargs', [dict(limit=True), dict(limit=0), dict(limit=21), dict(after_sequence=True),
                                   dict(after_sequence=-1), dict(after_sequence=2147483648)])
def test_client_rejects_local_cursor_errors(api, kwargs):
    with api['client']() as client, pytest.raises(ValueError):
        client.list_evidence(RUN, **kwargs)
    assert api['requests'] == []


@pytest.mark.parametrize('method,ids', [('list_approvals', ('bad',)), ('get_approval', (RUN, 'bad')),
    ('get_approval_review', ('bad', APPROVAL)), ('get_evidence', (RUN, 'bad')), ('list_evidence', ('bad',))])
def test_client_rejects_invalid_identifiers(api, method, ids):
    with api['client']() as client, pytest.raises(TypeError):
        getattr(client, method)(*ids)
    assert api['requests'] == []


@pytest.mark.parametrize('variable,args', [('FORGE_APPROVAL_READ_TOKEN', ['approval', 'list', str(RUN)]),
    ('FORGE_APPROVAL_REVIEW_TOKEN', ['approval', 'review', str(RUN), str(APPROVAL)])])
def test_missing_capability_is_safe_local_exit(api, monkeypatch, variable, args):
    monkeypatch.delenv(variable)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and result.stdout == '' and variable in result.stderr
    assert api['requests'] == []


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('width', [32, 80])
def test_plain_rich_and_narrow_output(case, width, monkeypatch, capsys):
    method, ids, args, suffix, body = case
    api_models = {
        'list_approvals': 'PendingApprovalListResponse', 'get_approval': 'ApprovalDetailResponse',
        'get_approval_review': 'ApprovalPatchReview', 'list_evidence': 'EvidenceListResponse',
        'get_evidence': 'EvidenceAttemptDetail',
    }
    from forge_cli import dto
    model = getattr(dto, api_models[method]).model_validate_json(json.dumps(body))

    class Terminal(StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('COLUMNS', str(width))
    monkeypatch.delenv('NO_COLOR', raising=False)
    for stream in (StringIO(), Terminal()):
        with redirect_stdout(stream):
            output.print_result(model, json_output=False, no_color=True)
        rendered = stream.getvalue()
        assert '\x1b' not in rendered
        assert str(ids[-1]) in rendered
        if method == 'get_approval_review':
            assert rendered.endswith(PATCH if stream.isatty() else '\n'.join(output._escape(line) for line in PATCH.split('\n')))
    assert capsys.readouterr() == ('', '')


def test_patch_json_raw_and_terminal_controls(monkeypatch):
    patch = PATCH + '+[red]\x1b[2J\r\x00\u202e\t  \n'
    model = ApprovalPatchReview.model_validate_json(json.dumps(review(patch)))
    stream = StringIO()
    with redirect_stdout(stream):
        output.print_result(model, json_output=True)
    assert json.loads(stream.getvalue())['patch'] == patch
    assert patch not in repr(model)
    stream = StringIO()
    with redirect_stdout(stream):
        output.print_result(model, json_output=False)
    text = stream.getvalue()
    assert '\x1b' not in text and '\r' not in text and '\x00' not in text and '\u202e' not in text
    assert text.endswith('  \n')


@pytest.mark.parametrize('kind', ['patch', 'preview', 'check'])
def test_interactive_untrusted_content(kind, monkeypatch):
    from forge_cli.dto import ApprovalDetailResponse

    unsafe = '[bold]Привет\x1b[2J\r\x00\u202e\u200b'
    if kind == 'patch':
        model = ApprovalPatchReview.model_validate_json(json.dumps(review(PATCH + unsafe)))
    elif kind == 'preview':
        model = ApprovalDetailResponse.model_validate_json(json.dumps(dict(DETAIL,
            preview=dict(PREVIEW, files=[dict(PREVIEW['files'][0], path=unsafe)]))))
    else:
        model = EvidenceAttemptDetail.model_validate_json(json.dumps(dict(EVIDENCE,
            checks=[dict(CHECK, path=unsafe)])))

    class Terminal(StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('COLUMNS', '32')
    stream = Terminal()
    with redirect_stdout(stream):
        output.print_result(model, json_output=False, no_color=True)
    text = stream.getvalue()
    assert '[bold]Привет' in text
    assert all(char not in text for char in ('\x1b', '\r', '\x00', '\u202e', '\u200b'))


def test_credential_values_never_reach_diagnostics(api, monkeypatch):
    import os

    token = os.environ['FORGE_APPROVAL_READ_TOKEN']
    api['status'] = 403
    api['body'] = {'error': {'code': token, 'message': token}}
    result = CliRunner().invoke(app, ['approval', 'list', str(RUN)])
    if token in result.output or token in repr(result.exception):
        pytest.fail('Credential leaked into diagnostics')
    assert result.exit_code == 4
    monkeypatch.setenv('FORGE_APPROVAL_READ_TOKEN', token + '\n')
    result = CliRunner().invoke(app, ['approval', 'list', str(RUN)])
    if token in result.output or token in repr(result.exception):
        pytest.fail('Malformed credential leaked into diagnostics')
    assert result.exit_code == 2
