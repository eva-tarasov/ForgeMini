"""Opt-in TCP approval acceptance on the dedicated synthetic fixture only."""
import argparse
import asyncio
import json
import logging
import os
import secrets
import shlex
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID, uuid4

import demo_agent_run as shared

ROOT = shared.ROOT
DB = shared.DB
CONTAINER = 'forge-week4-step06-4d6f653603'
BACKEND_PYTHON = Path('/Users/dev/Desktop/forge/backend/.venv/bin/python')
FILE = 'sample.txt'
BEFORE = b'before\noutside hunk\n'
AFTER = b'synthetic-approved-change\noutside hunk\n'
PATCH = '--- a/sample.txt\n+++ b/sample.txt\n@@ -1,1 +1,1 @@\n-before\n+synthetic-approved-change\n'
FINGERPRINT = sha256(b'forge:apply_patch:v1\0' + PATCH.encode()).hexdigest()
SYNC_SECONDS = 10
SESSION_SECONDS = 240
PHASE_SECONDS = 45
require = shared.require


def configuration():
    return dict(database=DB, container=CONTAINER, postgres='127.0.0.1:50088', postgres_version='17.11',
                alembic_revision='1d972cc34fbe', backend=shared.BASE_URL, provider='offline MockTransport',
                model_requests_per_run_max=4, request_bytes_max=16384, completion_tokens_max=512,
                scenario_seconds=SESSION_SECONDS, approval_phase_seconds=PHASE_SECONDS,
                fixture='temporary sample.txt only', cleanup='only current fixture rows')


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('mode', choices=('describe', 'check', 'tcp', 'interactive'))
    parser.add_argument('--approve-db')
    parser.add_argument('--confirm-fixture-write', action='store_true')
    args = parser.parse_args(argv)
    if args.mode in ('tcp', 'interactive') and (args.approve_db != DB or not args.confirm_fixture_write):
        parser.error('Execution requires the exact --approve-db and --confirm-fixture-write.')
    return args


def docker_output(*args):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise shared.DemoError('docker_unavailable_or_timeout') from None
    require(result.returncode == 0, 'docker_preflight_failed')
    return result.stdout


def launcher_environment():
    """Inspect only the named container. Never echo its environment or errors."""
    try:
        [container] = json.loads(docker_output('inspect', CONTAINER))
        env = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
        ports = container['NetworkSettings']['Ports']['5432/tcp']
        require(container['Name'] == '/' + CONTAINER and container['State']['Running'], 'wrong_container')
        require(any(p['HostPort'] == '50088' and p['HostIp'] in ('127.0.0.1', '0.0.0.0') for p in ports),
                'wrong_postgres_port')
        require(env.get('POSTGRES_USER') and env.get('POSTGRES_PASSWORD'),
                'container_database_or_credentials_unavailable')
        identity = docker_output('exec', CONTAINER, 'psql', '-U', env['POSTGRES_USER'], '-d', DB, '-At', '-c',
            "SELECT current_database(), current_setting('server_version_num'), version_num FROM alembic_version")
        require(identity.decode().strip() == DB + '|170011|1d972cc34fbe', 'database_identity_mismatch')
    except (KeyError, ValueError, TypeError):
        raise shared.DemoError('container_configuration_invalid') from None
    require(BACKEND_PYTHON.is_file(), 'backend_python_missing')
    child = shared.cli_environment()
    child.update(POSTGRES_HOST='127.0.0.1', POSTGRES_PORT='50088', POSTGRES_DB=DB,
                 FORGE_REPOSITORY_AGENT_TEST_DB=DB, POSTGRES_USER=env['POSTGRES_USER'],
                 POSTGRES_PASSWORD=env['POSTGRES_PASSWORD'], PYTHONPATH=str(ROOT / 'backend'),
                 FORGE_APPROVAL_CONTAINER_VERIFIED=CONTAINER,
                 LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false', LANGCHAIN_TRACING='false')
    return child


def launch(argv):
    args = arguments(argv)
    if args.mode == 'describe':
        print(json.dumps(configuration(), sort_keys=True))
        return 0
    try:
        env = launcher_environment()
        if args.mode == 'check':
            print(json.dumps(dict(environment='ready', database=DB, container=CONTAINER)))
            return 0
        os.chdir(ROOT)
        os.execve(str(BACKEND_PYTHON), [str(BACKEND_PYTHON), str(Path(__file__).resolve()), *argv], env)
    except shared.DemoError as error:
        print(json.dumps(dict(outcome='FAILED', failure=str(error), cleanup='not_started')))
        return 1
    except (OSError, ValueError):
        print(json.dumps(dict(outcome='FAILED', failure='launcher_environment_unusable', cleanup='not_started')))
        return 1


class Driver(shared.Driver):
    def __init__(self, httpx):
        super().__init__(httpx)
        self.credentials = {
            'FORGE_APPROVAL_READ_TOKEN': secrets.token_urlsafe(36),
            'FORGE_APPROVAL_REVIEW_TOKEN': secrets.token_urlsafe(36),
        }
        self.override = None

    def environment(self):
        return {**shared.cli_environment(), **(self.credentials if self.override is None else self.override)}

    @asynccontextmanager
    async def process(self, *command, json_output=True):
        process = await asyncio.create_subprocess_exec(
            str(shared.CLI), '--base-url', shared.BASE_URL, '--no-color',
            *(['--json'] if json_output else []), *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.environment(),
        )
        try:
            yield process
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)


class OfflineProvider:
    def __init__(self):
        self.calls = 0

    async def reply(self, request):
        import httpx
        require(request.method == 'POST' and str(request.url) == 'https://approval.invalid/v1/chat/completions',
                'unexpected_model_endpoint')
        require(len(request.content) <= 16384 and self.calls < 4, 'offline_model_budget_exceeded')
        body = json.loads(request.content)
        require(body.get('max_completion_tokens') == 512 and not body.get('stream'), 'model_request_budget_invalid')
        self.calls += 1
        index = self.calls - 1
        finish = 'tool_calls'
        if index < 2:
            name, arguments = ('read_file', {'path': FILE}) if index == 0 else ('apply_patch', {'patch': PATCH})
            message = dict(content=None, tool_calls=[dict(id=f'fixture-tool-{index}', type='function',
                function=dict(name=name, arguments=json.dumps(arguments)))])
        elif index == 2:
            finish, message = 'stop', dict(content='Synthetic operation reported. Independent verification pending.')
        else:
            require(body.get('response_format', {}).get('type') == 'json_schema', 'missing_structured_request')
            finish, message = 'stop', dict(content=json.dumps(dict(summary='Synthetic fixture observed',
                relevant_components=['repository'], assumptions=['Independent verification pending'], blocked_reason=None)))
        return httpx.Response(200, json=dict(id=f'chatcmpl-fixture-{index}', object='chat.completion', created=1,
            model=body['model'], choices=[dict(index=0, finish_reason=finish, message=dict(role='assistant', **message))],
            usage=dict(prompt_tokens=20, completion_tokens=10, total_tokens=30)))


def scoped_replacer(root, replacements, original):
    def replace(source, target, **kwargs):
        identity = root.stat()
        for key in ('src_dir_fd', 'dst_dir_fd'):
            require(key in kwargs, 'unscoped_filesystem_replace')
            directory = os.fstat(kwargs[key])
            require((directory.st_dev, directory.st_ino) == (identity.st_dev, identity.st_ino),
                    'filesystem_replace_outside_fixture')
        require(target == FILE and '/' not in str(source), 'filesystem_replace_outside_scope')
        result = original(source, target, **kwargs)
        replacements.append(target)
        return result
    return replace


async def settled(coordinator, execution):
    event = asyncio.Event()
    execution.add_done_callback(lambda _: event.set())
    await asyncio.wait_for(event.wait(), SYNC_SECONDS)
    require(coordinator._execution is None and not coordinator._active_runs._executions, 'execution_cleanup_incomplete')
    if not execution.cancelled():
        execution.exception()  # Observe expected rejection errors without printing them.


async def verify(driver, smoke, factory, binding, task_id, run_id):
    identity = smoke.VerificationBinding(UUID(run_id), UUID(task_id), binding.workspace_id, binding.baseline.revision)
    plan = smoke.VerificationPlan(uuid4(), identity, smoke.fingerprint_criteria(smoke.CRITERIA),
        (smoke.FileDigestCheck(uuid4(), 0, FILE, sha256(AFTER).hexdigest()),))
    access = smoke.TrustedFixtureAccess(plan_id=plan.plan_id, plan_fingerprint=plan.fingerprint, binding=identity,
        root=binding.root, read_scopes=binding.read_scopes, acceptance_criteria=smoke.CRITERIA)
    attempt_id = uuid4()
    if driver.allowed_requests is not None:
        driver.allowed_requests.add(('GET', f'/agent-runs/{run_id}/evidence/{attempt_id}'))
    record = await smoke.VerificationService(factory).verify(plan, access, attempt_id=attempt_id, isolated_fixture=True)
    require(record.verdict is smoke.VerificationVerdict.PASS and record.audit_complete, 'independent_verification_failed')
    page = await driver.cli('evidence_list', 'evidence', 'list', run_id, status=200)
    detail = await driver.cli('evidence_get', 'evidence', 'get', run_id, str(attempt_id), status=200)
    require(len(page['items']) == 1 and detail['verdict'] == 'pass' and detail['audit_complete']
            and detail['checks'][0]['outcome'] == 'match', 'persisted_evidence_invalid')
    return str(attempt_id)


@asynccontextmanager
async def broker(path, credentials):
    async def respond(reader, writer):
        try:
            writer.write(json.dumps(credentials).encode())
            await asyncio.wait_for(writer.drain(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_unix_server(respond, path=str(path))
    path.chmod(0o600)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


def terminal(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--session', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ['--']:
        args.command = args.command[1:]
    require(not any(x == '--base-url' or x.startswith('--base-url=') for x in args.command), 'fixed_backend_required')
    path = Path(args.session)
    require(path.name == 'capability.sock' and path.parent.name.startswith('forge-approval-'), 'invalid_session_socket')
    require(path.stat().st_uid == os.getuid() and path.parent.stat().st_mode & 0o077 == 0, 'unsafe_session_permissions')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(3)
        channel.connect(str(path))
        parts = []
        while True:
            part = channel.recv(4096)
            if not part:
                break
            parts.append(part)
            require(sum(map(len, parts)) < 4096, 'session_response_too_large')
    credentials = json.loads(b''.join(parts))
    require(set(credentials) == {'FORGE_APPROVAL_READ_TOKEN', 'FORGE_APPROVAL_REVIEW_TOKEN'}, 'invalid_session_response')
    os.execve(str(shared.CLI), [str(shared.CLI), '--base-url', shared.BASE_URL, *args.command],
              {**shared.cli_environment(), **credentials})


def terminal_command(path, *command):
    return shlex.join([str(shared.CLI.with_name('python')), str(Path(__file__).resolve()),
                       'terminal', '--session', str(path), '--', *command])


async def interactive_decision(path, driver, run_id, approval_id, choice, execution):
    print(f'READY {choice.upper()} phase, {PHASE_SECONDS} seconds. Run commands in Terminal 2.', flush=True)
    for command in [('approval', 'list', run_id), ('approval', 'get', run_id, approval_id),
                    ('approval', 'review', run_id, approval_id)]:
        print(terminal_command(path, *command), flush=True)
    if choice == 'approve':
        print('The next command explicitly authorizes a write to temporary synthetic sample.txt.', flush=True)
        command = ('approval', 'approve', run_id, approval_id, '--reviewed-patch-sha256', FINGERPRINT, '--confirm')
    else:
        command = ('approval', 'reject', run_id, approval_id, '--confirm')
    before = len(driver.requests)
    print(terminal_command(path, *command), flush=True)
    done, _ = await asyncio.wait({execution}, timeout=PHASE_SECONDS)
    require(bool(done), 'owner_decision_deadline')
    expected = ('POST', f'/agent-runs/{run_id}/approvals/{approval_id}/decision', 200)
    async with asyncio.timeout(SYNC_SECONDS):
        while expected not in driver.requests[before:]:
            driver.request_recorded.clear()
            await driver.request_recorded.wait()
    print('Decision response observed. Wait for the next Terminal 2 commands.', flush=True)


async def security_checks(driver, run_id, approval_id, foreign_run):
    credentials = driver.credentials
    try:
        driver.override = {}
        await driver.cli('missing_read', 'approval', 'list', run_id, exit_code=2)
        await driver.cli('missing_review', 'approval', 'review', run_id, approval_id, exit_code=2)
        driver.override = {**credentials, 'FORGE_APPROVAL_REVIEW_TOKEN': credentials['FORGE_APPROVAL_READ_TOKEN']}
        await driver.cli('read_cannot_review', 'approval', 'review', run_id, approval_id, status=403, exit_code=4)
        await driver.cli('read_cannot_decide', 'approval', 'reject', run_id, approval_id, '--confirm', status=403, exit_code=4)
    finally:
        driver.override = None
    await driver.cli('foreign_workspace', 'approval', 'list', foreign_run, status=404, exit_code=4)
    await driver.cli('foreign_pair', 'approval', 'get', foreign_run, approval_id, status=404, exit_code=4)
    await driver.cli('wrong_approval', 'approval', 'get', run_id, str(uuid4()), status=404, exit_code=4)


async def scenario(driver, smoke, factory, workspace_id, foreign_run, choice, root, mode, report):
    import httpx
    import uvicorn
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr
    from app.domain.capabilities import Capability
    from app.services.approved_repository_agent_runner import ApprovedRepositoryAgentRunner, ApprovedRepositoryFixtureBinding
    from app.services.local_approval_access import LocalApprovalAccessBinding
    from app.services.pending_approvals import PendingApprovals
    from app.tools.repository import writer

    target = root / FILE
    target.write_bytes(BEFORE)
    sentinel = root.parent / f'{choice}-outside.txt'
    sentinel.write_bytes(b'outside write scope')
    binding = ApprovedRepositoryFixtureBinding(
        UUID(workspace_id), root, smoke.FixtureBaseline('approval-fixture-v1', {FILE: BEFORE.decode()}),
        frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE}),
        (smoke.PathScope(FILE),), (smoke.PathScope(FILE),),
    )
    provider = OfflineProvider()
    published = asyncio.Event()
    publish = PendingApprovals.publish
    replacements = []

    def observe(pending, operation):
        publish(pending, operation)
        published.set()

    access = LocalApprovalAccessBinding(binding.workspace_id, SecretStr(driver.credentials['FORGE_APPROVAL_READ_TOKEN']),
        review_token=SecretStr(driver.credentials['FORGE_APPROVAL_REVIEW_TOKEN']))
    transport = httpx.MockTransport(provider.reply)
    with httpx.Client(transport=smoke.ClosedSyncTransport(), trust_env=False) as sync_client:
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as async_client:
            model = ChatOpenAI(model='gpt-4o-mini', api_key=secrets.token_urlsafe(36), max_retries=0,
                base_url='https://approval.invalid/v1', http_client=sync_client, http_async_client=async_client,
                timeout=10, max_completion_tokens=512, use_responses_api=False, openai_proxy='')
            app = smoke.create_app(session_factory=factory, approval_access_binding=access,
                execution_binding=smoke.RunExecutionBinding(binding.workspace_id, lambda sf, rid:
                    ApprovedRepositoryAgentRunner(sf, model, binding, run_id=rid, wait_seconds=PHASE_SECONDS)))
            driver.allowed_requests = None
            driver.attach(app)
            response_gate = asyncio.Event()
            lose_next_response = False

            @app.middleware('http')
            async def hold_recorded_response(request, call_next):
                response = await call_next(request)
                if (lose_next_response and request.method == 'POST'
                        and request.url.path.endswith('/decision') and response.status_code == 200):
                    await asyncio.wait_for(response_gate.wait(), SYNC_SECONDS)
                return response

            with patch.object(PendingApprovals, 'publish', observe), patch.object(
                writer.os, 'replace', scoped_replacer(root, replacements, os.replace)
            ):
                async with shared.serve(app, uvicorn):
                    task_id, run_id = await shared.make_run(driver, smoke, workspace_id, choice)
                    await driver.cli('start_' + choice, 'run', 'start', run_id, status=202)
                    execution = app.state.run_coordinator._execution.task
                    await asyncio.wait_for(published.wait(), SYNC_SECONDS)
                    metadata = await driver.cli('waiting_' + choice, 'run', 'get', run_id, status=200)
                    require(metadata['status'] == 'waiting_approval', 'approval_not_committed')
                    require(target.read_bytes() == BEFORE and not replacements, 'write_before_decision')
                    listing = await driver.cli('approval_list', 'approval', 'list', run_id, status=200)
                    require(len(listing['items']) == 1, 'expected_one_pending_approval')
                    approval_id = listing['items'][0]['id']
                    detail = await driver.cli('approval_preview', 'approval', 'get', run_id, approval_id, status=200)
                    require(detail['preview']['state'] == 'live_at_read' and not detail['preview']['content_included']
                            and detail['preview']['files'][0]['path'] == FILE, 'structural_preview_invalid')
                    review = await driver.cli('exact_review', 'approval', 'review', run_id, approval_id, status=200)
                    require(review['patch'] == PATCH and review['patch_sha256'] == FINGERPRINT, 'review_integrity_invalid')
                    if mode == 'tcp':
                        await security_checks(driver, run_id, approval_id, foreign_run)
                        await driver.cli('stale_fingerprint', 'approval', 'approve', run_id, approval_id,
                            '--reviewed-patch-sha256', sha256(b'stale').hexdigest(), '--confirm', status=409, exit_code=4)
                        still_pending = await driver.cli('still_pending', 'approval', 'get', run_id, approval_id, status=200)
                        require(still_pending['status'] == 'pending' and still_pending['decided_at'] is None
                                and target.read_bytes() == BEFORE and not replacements, 'stale_decision_changed_state')
                    if mode == 'interactive':
                        driver.allowed_requests = {
                            ('GET', f'/agent-runs/{run_id}'), ('GET', f'/agent-runs/{run_id}/result'),
                            ('GET', f'/agent-runs/{run_id}/events'), ('GET', f'/agent-runs/{run_id}/approvals'),
                            ('GET', f'/agent-runs/{run_id}/approvals/{approval_id}'),
                            ('GET', f'/agent-runs/{run_id}/approvals/{approval_id}/review'),
                            ('POST', f'/agent-runs/{run_id}/approvals/{approval_id}/decision'),
                            ('GET', f'/agent-runs/{run_id}/evidence'),
                        }
                        async with broker(root.parent / 'capability.sock', driver.credentials):
                            await interactive_decision(root.parent / 'capability.sock', driver, run_id, approval_id, choice, execution)
                    elif choice == 'cancel':
                        await driver.cli('cancel_pending', 'run', 'cancel', run_id, status=200)
                    else:
                        decision = 'reject' if choice == 'lost-response' else choice
                        command = ['approval', decision, run_id, approval_id, '--confirm']
                        if choice == 'approve':
                            command += ['--reviewed-patch-sha256', review['patch_sha256']]
                        if choice == 'lost-response':
                            lose_next_response = True
                            before = len(driver.requests)
                            try:
                                await driver.cli('decision_response_timeout', *command, status=200, exit_code=3)
                            finally:
                                response_gate.set()
                                lose_next_response = False
                            require(len(driver.requests[before:]) == 1, 'ambiguous_decision_was_retried')
                        else:
                            recorded = await driver.cli('record_' + choice, *command, status=200)
                            require(recorded['recorded'] is True and recorded['decision'] ==
                                    ('approved' if choice == 'approve' else 'rejected'), 'decision_not_recorded')
                    # Completion is observed through the actual backend-owned task, never induced here.
                    done, _ = await asyncio.wait({execution}, timeout=SYNC_SECONDS)
                    require(bool(done), 'execution_did_not_settle')
                    await settled(app.state.run_coordinator, execution)
                    await driver.cli('repeat_decision_conflict', 'approval', 'reject', run_id, approval_id,
                                     '--confirm', status=409, exit_code=4)
                    await driver.cli('terminal_approve_conflict', 'approval', 'approve', run_id, approval_id,
                        '--reviewed-patch-sha256', FINGERPRINT, '--confirm', status=409, exit_code=4)
                    result = await driver.cli('preliminary_or_failure', 'run', 'result', run_id, status=200)
                    if choice == 'approve':
                        require(not execution.cancelled() and execution.exception() is None, 'approved_execution_failed')
                        require(result['run_status'] == 'verifying' and result['preliminary_status'] == 'ready_for_verification',
                                'missing_preliminary_result')
                        require(target.read_bytes() == AFTER and replacements == [FILE] and provider.calls == 4,
                                'approved_write_or_model_count_invalid')
                        # Only this trusted test harness performs independent verification.
                        attempt_id = await verify(driver, smoke, factory, binding, task_id, run_id)
                        result = await driver.cli('verified_result', 'run', 'result', run_id, status=200)
                        require(result['run_status'] == 'succeeded' and result['verification_verdict'] == 'pass',
                                'final_verification_not_pass')
                        await driver.cli('succeeded_approve_conflict', 'approval', 'approve', run_id, approval_id,
                            '--reviewed-patch-sha256', FINGERPRINT, '--confirm', status=409, exit_code=4)
                        if mode == 'interactive':
                            driver.allowed_requests = {
                                ('GET', f'/agent-runs/{run_id}/result'), ('GET', f'/agent-runs/{run_id}/evidence'),
                                ('GET', f'/agent-runs/{run_id}/evidence/{attempt_id}'),
                            }
                            async with broker(root.parent / 'capability.sock', driver.credentials):
                                for cmd in [('evidence', 'list', run_id), ('evidence', 'get', run_id, attempt_id), ('run', 'result', run_id)]:
                                    print(terminal_command(root.parent / 'capability.sock', *cmd), flush=True)
                                print('Inspect these in Terminal 2. Press Return in Terminal 1 to begin the reject phase.', flush=True)
                                async with asyncio.timeout(PHASE_SECONDS):
                                    await shared.wait_for_terminal_shutdown(sys.stdin.fileno())
                        require(replacements == [FILE], 'patch_applied_again')
                    else:
                        expected = 'cancelled' if choice == 'cancel' else 'failed'
                        require(result['run_status'] == expected and result['verification_verdict'] is None
                                and result['preliminary_status'] is None, 'unexpected_rejected_result')
                        if choice in ('reject', 'lost-response'):
                            require(result['execution_failure_code'] == 'graph_execution_failed', 'unexpected_rejection_failure')
                        require(target.read_bytes() == BEFORE and not replacements and provider.calls == 2,
                                'rejected_run_wrote_fixture')
                        page = await driver.cli('no_rejected_evidence', 'evidence', 'list', run_id, status=200)
                        require(not page['items'], 'unexpected_rejected_evidence')
                    driver.allowed_requests = None
                    stored = await driver.cli('stored_approval', 'approval', 'get', run_id, approval_id, status=200)
                    require(stored['status'] == ('approved' if choice == 'approve' else 'rejected'), 'decision_not_persisted')
                    async with factory() as session:
                        events = list((await session.scalars(smoke.select(smoke.AgentEventModel).where(
                            smoke.AgentEventModel.agent_run_id == UUID(run_id)))).all())
                    decisions = [event for event in events if event.event_type == 'approval_decided']
                    require(len(decisions) == 1, 'duplicate_persisted_decisions')
                    if choice != 'approve':
                        finished = [event for event in events if event.event_type == 'tool_call_finished'
                                    and event.payload.get('tool_name') == 'apply_patch']
                        require(len(finished) == 1 and finished[0].payload.get('error_code') ==
                                ('cancelled' if choice == 'cancel' else 'approval_rejected'), 'missing_terminal_write_audit')
                    require(sentinel.read_bytes() == b'outside write scope' and list(root.iterdir()) == [target],
                            'fixture_scope_changed')
                    report['scenarios'].append(dict(path=choice, run_status=result['run_status'],
                        decision=stored['status'], recorded_events=len(decisions), replacements=len(replacements),
                        model_requests=provider.calls, verification=result['verification_verdict'],
                        evidence_checked=choice == 'approve', ambiguous_post_no_retry=choice == 'lost-response', execution_cleanup='observed', outside_scope_unchanged=True))
                    if mode == 'interactive' and choice == 'reject':
                        driver.allowed_requests = {
                            ('GET', f'/agent-runs/{run_id}/result'),
                            ('GET', f'/agent-runs/{run_id}/evidence'),
                            ('GET', f'/agent-runs/{run_id}/approvals/{approval_id}'),
                        }
                        async with broker(root.parent / 'capability.sock', driver.credentials):
                            print(terminal_command(root.parent / 'capability.sock', 'run', 'result', run_id), flush=True)
                            print('Inspect rejected result. Press Return in Terminal 1 to stop and clean up.', flush=True)
                            async with asyncio.timeout(PHASE_SECONDS):
                                await shared.wait_for_terminal_shutdown(sys.stdin.fileno())
                require(app.state.run_coordinator._execution is None, 'shutdown_execution_pending')


async def run_demo(mode, report):
    if mode == 'interactive':
        require(sys.stdin.isatty() and sys.stdout.isatty(), 'interactive_requires_terminal')
    require(os.getenv('FORGE_APPROVAL_CONTAINER_VERIFIED') == CONTAINER, 'use_reviewed_owner_wrapper')
    import httpx
    import uvicorn
    from app.db.models.execution import ArtifactModel
    from scripts import week4_live_smoke as smoke

    require(Path(smoke.__file__).resolve() == ROOT / 'backend/scripts/week4_live_smoke.py', 'backend_source_mismatch')
    require(shared.CLI.is_file() and os.access(shared.CLI, os.X_OK), 'forge_not_installed')
    with shared.loopback_socket():
        pass
    engine = smoke.create_engine(smoke.load_db_settings())
    factory = smoke.create_session_factory(engine)
    lock = factory()
    tables = (smoke.WorkspaceModel, smoke.TaskModel, smoke.AgentRunModel, smoke.AgentEventModel,
              smoke.ToolCallModel, smoke.ApprovalModel, ArtifactModel)
    names = []
    driver = Driver(httpx)
    report.update(steps=driver.steps, scenarios=[], fixture=str(uuid4()))
    try:
        await shared.guard_empty_database(smoke, factory, tables)
        async with asyncio.timeout(5):
            require(await lock.scalar(smoke.text('SELECT pg_try_advisory_xact_lock(516058066)')) is True,
                    'integration_session_already_running')
        await shared.guard_empty_database(smoke, factory, tables)
        try:
            with TemporaryDirectory(prefix='forge-approval-', dir='/tmp') as directory, smoke.tracing_context(enabled=False, parent=False):
                parent = Path(directory).resolve()
                async with asyncio.timeout(SESSION_SECONDS):
                    bootstrap = smoke.create_app(session_factory=factory)
                    driver.attach(bootstrap)
                    async with shared.serve(bootstrap, uvicorn):
                        choices = ('approve', 'reject', 'cancel', 'lost-response') if mode == 'tcp' else ('approve', 'reject')
                        workspaces = {}
                        for choice in (*choices, 'foreign'):
                            name = f"CLI06 {report['fixture']} {choice}"
                            names.append(name)
                            workspace = await driver.cli('workspace_' + choice, 'workspace', 'create', '--name', name, status=201)
                            workspaces[choice] = workspace['id']
                        _, foreign_run = await shared.make_run(driver, smoke, workspaces['foreign'], 'foreign')
                    for choice in choices:
                        root = parent / choice
                        root.mkdir(mode=0o700)
                        await scenario(driver, smoke, factory, workspaces[choice], foreign_run, choice, root, mode, report)
        finally:
            report['cleanup'] = 'unconfirmed'
            pending = [task for task in asyncio.all_tasks() if task.get_name() in
                       ('forge-http-agent-run', 'forge-http-shutdown-cancel') and not task.done()]
            require(not pending, 'backend_tasks_pending_cleanup_refused')
            for name in names:
                await shared.cleanup_workspace(smoke, factory, name, ArtifactModel)
            await shared.guard_empty_database(smoke, factory, tables)
            report['cleanup'] = 'completed'
    finally:
        async with asyncio.timeout(5):
            await lock.close()
            await engine.dispose()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'launch':
        return launch(argv[1:])
    if argv and argv[0] == 'terminal':
        try:
            terminal(argv[1:])
        except (shared.DemoError, OSError, ValueError):
            print('Approval session unavailable or invalid.', file=sys.stderr)
            return 2
        return 0
    args = arguments(argv)
    if args.mode == 'describe':
        print(json.dumps(configuration(), sort_keys=True))
        return 0
    if args.mode == 'check':
        print(json.dumps(dict(outcome='FAILED', failure='check_requires_wrapper', cleanup='not_started')))
        return 2
    logging.disable(logging.CRITICAL)
    report = dict(mode=args.mode, outcome='FAILED', cleanup='not_started')
    try:
        asyncio.run(run_demo(args.mode, report))
    except shared.DemoError as error:
        report['failure'] = str(error)
    except TimeoutError:
        report['failure'] = 'bounded_deadline_exceeded'
    except KeyboardInterrupt:
        report['failure'] = 'owner_interrupted'
    except Exception:
        report['failure'] = 'configuration_or_runtime_error'
    else:
        report['outcome'] = 'SESSION_ENDED' if args.mode == 'interactive' else 'PASS'
        if args.mode == 'interactive':
            report['manual_verification'] = 'owner_pending'
    print(json.dumps(report, sort_keys=True))
    return int(report['outcome'] == 'FAILED')


if __name__ == '__main__':
    raise SystemExit(main())
