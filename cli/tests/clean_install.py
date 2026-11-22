"""Build and exercise a release wheel in a disposable environment outside the checkout."""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
STAGE_SECONDS = 180
CLI_SECONDS = 15
COMMANDS = {
    'workspace': ('create', 'list'),
    'task': ('create', 'get'),
    'run': ('create', 'get', 'start', 'cancel', 'events', 'result', 'watch'),
}


class SmokeFailure(Exception):
    def __init__(self, stage, reason, *, blocked=False):
        self.stage = stage
        self.reason = reason
        self.blocked = blocked
        super().__init__(reason)


def require(condition, stage, reason):
    if not condition:
        raise SmokeFailure(stage, reason)


def environment(*, installer=False):
    keys = ['PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT']
    if installer:
        keys += ['HOME', 'TMPDIR', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE',
                 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_TRUSTED_HOST',
                 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(PYTHONNOUSERSITE='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1',
               PIP_NO_CACHE_DIR='1', PIP_CONFIG_FILE=os.devnull, TERM='dumb', NO_COLOR='1')
    return env


def run_stage(stage, command, cwd, *, installer=False, expected=0, seconds=CLI_SECONDS):
    env = environment(installer=installer)
    if installer:
        env['TMPDIR'] = str(cwd.parent)
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise SmokeFailure(stage, 'process_start_failed', blocked=True) from None
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except BaseException:
        # Kill the entire disposable subprocess group, including pip build children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode != expected:
        data = (stdout + stderr).lower()
        unavailable = any(marker in data for marker in (
            b'no matching distribution', b'could not find a version', b'connection refused',
            b'network is unreachable', b'name resolution', b'nodename nor servname',
            b'failed to establish', b'could not fetch url', b'proxyerror', b'connection broken',
        ))
        raise SmokeFailure(stage, 'unexpected_exit', blocked=installer and unavailable)
    return stdout, stderr


def inspect_wheel(path):
    with ZipFile(path) as archive:
        names = archive.namelist()
    require('forge_cli/main.py' in names, 'wheel', 'missing_entry_module')
    require(all(name.startswith(('forge_cli/', 'forge_cli-0.1.0.dist-info/')) for name in names),
            'wheel', 'unexpected_wheel_payload')


IMPORT_PROBE = '''
import importlib, importlib.metadata, json, os, pathlib, pkgutil, sys
import forge_cli
root = pathlib.Path(sys.argv[1]).resolve()
origin = pathlib.Path(forge_cli.__file__).resolve()
assert not pathlib.Path.cwd().resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve())
assert not os.environ.get("PYTHONPATH")
assert origin.is_relative_to(root)
assert importlib.metadata.version("forge-cli") == "0.1.0"
for module in pkgutil.walk_packages(forge_cli.__path__, "forge_cli."):
    importlib.import_module(module.name)
assert "app" not in sys.modules and "scripts" not in sys.modules
print(json.dumps({"version": "0.1.0", "imports_from_venv": True, "backend_imported": False,
    "python": sys.version.split()[0],
    "dependencies": {name: importlib.metadata.version(name) for name in ("typer", "rich", "httpx", "pydantic")}}))
'''


def exercise_installed(venv, outside, report):
    python = str(venv / 'bin/python')
    forge = str(venv / 'bin/forge')
    stdout, stderr = run_stage('imports', [python, '-c', IMPORT_PROBE, str(venv), str(ROOT)], outside)
    require(not stderr, 'imports', 'unexpected_stderr')
    report.update(json.loads(stdout))
    stdout, stderr = run_stage('version', [forge, '--version'], outside)
    require(stdout == b'0.1.0\n' and not stderr, 'version', 'invalid_version_streams')
    help_commands = [[], ['health']]
    for group, commands in COMMANDS.items():
        help_commands.append([group])
        help_commands.extend([group, command] for command in commands)
    for command in help_commands:
        stdout, stderr = run_stage('help', [forge, *command, '--help'], outside)
        require(b'Usage:' in stdout and not stderr and b'\x1b' not in stdout, 'help', 'invalid_help_streams')
    report['help_checks'] = len(help_commands)

    requests = []

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b'{"status":"ok"}'
            self.send_response(200 if self.path == '/health' else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(('127.0.0.1', 0), HealthHandler)
    server.timeout = 2
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        stdout, stderr = run_stage('health_json', [forge, '--base-url', base, '--json', 'health'], outside)
        require(json.loads(stdout) == {'status': 'ok'} and not stderr and b'\x1b' not in stdout,
                'health_json', 'invalid_json_streams')
        stdout, stderr = run_stage('health_plain', [forge, '--base-url', base, '--no-color', 'health'], outside)
        require(stdout == b'status=ok\n' and not stderr, 'health_plain', 'invalid_plain_streams')
        before = len(requests)
        stdout, stderr = run_stage('invalid_uuid', [forge, '--base-url', base, '--json', 'run', 'get', 'invalid'],
                                   outside, expected=2)
        require(not stdout and stderr and b'\x1b' not in stderr and b'Traceback' not in stderr,
                'invalid_uuid', 'unsafe_error_streams')
        require(len(requests) == before and requests == ['/health', '/health'], 'health', 'unexpected_http_requests')
        report.update(health_requests=len(requests), invalid_input_exit=2, streams_separated=True,
                      outside_checkout=True, source_pythonpath=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        require(not thread.is_alive(), 'cleanup', 'server_thread_pending')
    run_stage('dependencies', [python, '-m', 'pip', 'check'], outside)
    report['dependency_check'] = 'PASS'


def smoke(report):
    temporary = None
    try:
        with TemporaryDirectory(prefix='forge-cli-release-', dir='/tmp') as directory:
            temporary = Path(directory).resolve()
            outside = temporary / 'outside'
            outside.mkdir()
            source = temporary / 'source'
            source.mkdir()
            copytree(ROOT / 'cli/src', source / 'src', ignore=lambda _, names: [n for n in names if n == '__pycache__'])
            for name in ('pyproject.toml', 'README.md'):
                (source / name).write_bytes((ROOT / 'cli' / name).read_bytes())
            venv = temporary / 'venv'
            run_stage('venv', [sys.executable, '-m', 'venv', str(venv)], outside, seconds=60)
            python = str(venv / 'bin/python')
            wheels = temporary / 'wheels'
            run_stage('build', [python, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(wheels), str(source)],
                      outside, installer=True, seconds=STAGE_SECONDS)
            artifacts = list(wheels.glob('forge_cli-0.1.0-*.whl'))
            require(len(artifacts) == 1, 'wheel', 'expected_one_versioned_wheel')
            wheel = artifacts[0]
            inspect_wheel(wheel)
            report.update(wheel=wheel.name, wheel_sha256=sha256(wheel.read_bytes()).hexdigest())
            run_stage('install', [python, '-m', 'pip', 'install', str(wheel)], outside,
                      installer=True, seconds=STAGE_SECONDS)
            exercise_installed(venv, outside, report)
    finally:
        report['cleanup'] = 'completed' if temporary is not None and not temporary.exists() else 'unconfirmed'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    report = dict(outcome='FAILED', cleanup='not_started', package_index_access='build and runtime dependencies may download')
    try:
        smoke(report)
    except SmokeFailure as error:
        report.update(outcome='BLOCKED' if error.blocked else 'FAIL', stage=error.stage, reason=error.reason)
    except subprocess.TimeoutExpired:
        report.update(outcome='BLOCKED', reason='bounded_stage_timeout')
    except KeyboardInterrupt:
        report.update(outcome='FAIL', reason='interrupted')
    except Exception:
        report.update(outcome='FAIL', reason='environment_or_runtime_error')
    else:
        report['outcome'] = 'PASS'
    print(json.dumps(report, sort_keys=True))
    return 0 if report['outcome'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
