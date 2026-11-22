# Forge CLI

Forge CLI is a standalone HTTP client for the ForgeMini backend. Package version is `0.1.0`. Python 3.11 or later is required. The current release checks use Python 3.13. Support for the declared minimum Python version still needs a separate interpreter run.

The backend owns persistence, authorization, execution and verification. Installing the CLI requires Typer, Rich, HTTPX and Pydantic from its declared dependencies. Backend Python packages are unnecessary for normal CLI use.

## Installation

For development in a fresh checkout, create a dedicated environment and install the CLI with its test dependencies.

```sh
python3 -m venv cli/.venv
cli/.venv/bin/python -m pip install -e './cli[dev]'
cli/.venv/bin/forge --help
cli/.venv/bin/forge --version
```

For a distributed wheel, use `python -m pip install forge_cli-0.1.0-py3-none-any.whl` in the receiving environment. Pip installs the runtime dependencies declared by the wheel. The version command prints `0.1.0`.

The reproducible clean-wheel acceptance launcher builds from the current source, installs the wheel and its dependencies into a fresh temporary venv, and exercises the installed command outside the repository.

```sh
cd <path-to-your-ForgeMini-checkout>
cli/.venv/bin/python cli/tests/clean_install.py
```

This uses normal pip build isolation. Build dependencies and runtime dependencies may download from the environment-configured package index, or pip's default index. User pip configuration files are disabled to prevent redirected installation targets. Index access is required when those packages are unavailable locally. An unavailable index produces BLOCKED. Existing editable packages are never substituted for the installation.

All build sources, wheel artifacts, venv and working directory are under a new `/tmp/forge-cli-release-*` directory. A deterministic HTTP server binds an ephemeral IPv4 loopback port for two health requests. The launcher checks all command help, version, package import origin, dependencies, JSON/plain streams and local UUID rejection. It removes the server and temporary directory before printing its final JSON report. Pip stages each have a 180-second limit, venv creation 60 seconds, and CLI probes 15 seconds each. Subprocess groups are terminated if interrupted or timed out. Package-index output is captured and omitted from diagnostics to protect configured credentials.

## Configuration

Global options precede the command. Base URL precedence is `--base-url`, then `FORGE_BASE_URL`, then `http://127.0.0.1:8000`. Use an absolute HTTP(S) URL with a host and optional port/path. Credentials embedded in a URL, query strings, fragments and control characters are rejected locally.

```sh
cli/.venv/bin/forge --base-url http://127.0.0.1:8000 --json health
cli/.venv/bin/forge --no-color workspace list
```

The CLI sends no credentials: the prototype backend is meant to run on loopback (`127.0.0.1`) and has no authentication yet (see "Security boundaries"). The client disables environment proxy handling and redirects, uses a five-second HTTP timeout with a two-second connect timeout, and performs no automatic request retries.

## Commands

| Command | Input and behavior |
|---|---|
| `forge solve --repo PATH --goal TEXT --test CMD` | ForgeMini one-shot: create/reuse a workspace, create a task, start a run, stream agent steps, print the verified report. Optional `--image`, `--name`, `--timeout`, `--no-watch`. Exit code 0 only when the result was independently verified; 1 when not verified; 6 on timeout. |
| `forge report RUN_UUID` | What happened and why: verdict, evidence (tests without/with the change), agent explanation. |
| `forge patch RUN_UUID` | Print the agent's unified diff (`forge patch UUID > fix.patch`). |
| `forge health` | Read backend health. |
| `forge workspace create --name NAME` | Create a workspace. |
| `forge workspace list` | Read workspace list. |
| `forge task create --workspace-id UUID --goal TEXT --criterion TEXT` | Repeat `--criterion` for 1–10 criteria. Optional `--idempotency-key UUID` is forwarded. |
| `forge task get UUID` | Read a Task. |
| `forge run create --task-id UUID` | Create/retrieve the initial queued attempt. |
| `forge run get UUID` | Read AgentRun metadata. |
| `forge run start UUID` | Request execution, report HTTP 202 acceptance. |
| `forge run cancel UUID` | Request cancellation, report cancelled metadata. |
| `forge run events UUID` | Read one event page, optional `--after-sequence` and `--limit`. |
| `forge run result UUID` | Read preliminary/result/verification/usage snapshot. |
| `forge run watch UUID` | Bounded GET-only observation. |

UUIDs in this reference identify values returned by prior commands. For a fully executable fixture demonstration with generated IDs, use the guarded launcher below. All command-specific help works offline.

## Lifecycle, observation and verification

`Task != AgentRun`. A Task describes the goal and criteria. `run create` creates its initial queued attempt with HTTP 201, or returns the existing initial queued attempt with HTTP 200. It does not start execution. `run start` reports acceptance. Read metadata/events/result to observe execution.

The supported local coordinator binds one trusted workspace and owns execution inside one backend process. The default app has no execution binding and returns 503 for start/cancel. A duplicate start conflicts with 409 and another active run can consume the local capacity. Cancellation persists cancelled metadata and signals active execution. Repeated cancellation of an already cancelled run is safe. Transport failures on mutations leave an uncertain outcome. Read the current state before choosing any further action.

`run watch` defaults to an interval of 2 seconds (1–30), a timeout of 120 seconds (1–600) and cursor 0. Each cycle reads metadata and one page of at most 50 events. It stops after draining a terminal or waiting_approval state. Ctrl-C stops observation with exit 130 and leaves backend cancellation to an explicit command. In JSON mode, interruption or timeout emits no successful snapshot. Human output may already contain observed pages. The polling deadline and cycle count are bounded. An in-flight HTTP call may finish after the polling deadline under the client's HTTP timeout.

Event pages accept limit 1–100 (default 50) and use an integer cursor in 0–2147483647. Sequence gaps are valid. The client checks monotonic progression, next-cursor consistency and has_more semantics. Empty pages keep the input cursor.

`ready_for_verification` describes the agent's *preliminary* result; it is a claim, not a success. After the agent finishes, Forge itself re-runs the tests in a fresh container and records the verdict (`pass` / `fail` / `inconclusive`) with evidence (tests without and with the change). Only that verdict can set `run_status=succeeded` — the agent never can. `forge report RUN_UUID` prints the verdict and the evidence.

## Output and errors

Interactive terminals use Rich tables or labelled panels. Narrow layouts retain full identifiers and data. Non-TTY output is deterministic and escaped. Global `--no-color` disables color. Non-empty `NO_COLOR` is respected. Global `--json` bypasses Rich, writes successful DTO data as JSON and preserves raw patch data as a JSON string. Success goes to stdout and diagnostics to stderr.

Untrusted names, paths, diffs and text cannot become Rich markup or terminal commands. Human output escapes ANSI, control characters, unsafe bidi and Unicode format controls. Numeric booleans, inconsistent metadata, foreign IDs and malformed successful responses are rejected where the corresponding strict contracts require it. Error messages omit backend bodies, untrusted error fields, capabilities and private patches.

| Exit | Meaning |
|---|---|
| 0 | Successful result or help/version. |
| 2 | Local input/configuration/capability validation failure. |
| 3 | Transport failure. Mutation effect may be uncertain. |
| 4 | Unexpected HTTP status, including controlled backend errors. |
| 5 | Invalid successful response. |
| 6 | Watch observation deadline expired. |
| 130 | Watch interrupted by Ctrl-C. |

Usage fields expose known token/cost totals and completeness flags. Unknown values differ from a recorded zero. Provider cost has no guaranteed cap or completeness.

## Demo

`scripts/demo/run.sh` (repository root) fixes a real bug in a small demo shop repository end to end: start the server (`scripts/start.sh`) in one terminal, then run the script in another. It builds the target repository with `scripts/demo/make_demo_repo.sh` (or uses `DEMO_REPO_URL`), calls `forge solve`, and shows the verified report and patch. Modes: `--issue N`, `--all`, `--auto` (timed pauses for recording), `--fast` (no pauses; used by the tests). Run it with `--help` for details.

## Security boundaries and limitations

The backend has no authentication or authorization: run it on loopback only and do not expose the port. The agent never touches your working tree — it works on a copy inside a throw-away Docker container without network access, and Forge re-runs the tests in a second clean container before reporting a verdict. The CLI itself adds no isolation. Durable restart/resume of an interrupted run, a queue/worker and GitHub pull-request delivery are not implemented yet.

## Verification of the CLI itself

From the repository root, run the offline test suite and the dependency check. The clean-install launcher is opt-in; it builds the wheel and exercises the installed command in a fresh temporary venv.

```sh
cli/.venv/bin/python -m pytest -q cli/tests
cli/.venv/bin/python -m pip check
cli/.venv/bin/python cli/tests/clean_install.py
```
