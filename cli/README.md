# Forge CLI v0.1

Forge CLI is a standalone HTTP client for the existing Forge backend. Package version is `0.1.0`. Python 3.11 or later is required. The current release checks use Python 3.13. Support for the declared minimum Python version still needs a separate interpreter run.

The backend owns persistence, authorization, execution and verification. Installing the CLI requires Typer, Rich, HTTPX and Pydantic from its declared dependencies. Backend Python packages are unnecessary for normal CLI use. See [release evidence and handoff](RELEASE_V0_1.md) for current and historical acceptance results.

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
cd /Users/dev/Desktop/myFirstForge-week4cli
cli/.venv/bin/python cli/tests/clean_install.py
```

This uses normal pip build isolation. Build dependencies and runtime dependencies may download from the environment-configured package index, or pip's default index. User pip configuration files are disabled to prevent redirected installation targets. Index access is required when those packages are unavailable locally. An unavailable index produces BLOCKED. Existing editable packages are never substituted for the installation.

All build sources, wheel artifacts, venv and working directory are under a new `/tmp/forge-cli-release-*` directory. A deterministic HTTP server binds an ephemeral IPv4 loopback port for two health requests. The launcher checks all command help, version, package import origin, dependencies, JSON/plain streams and local UUID rejection. It removes the server and temporary directory before printing its final JSON report. Pip stages each have a 180-second limit, venv creation 60 seconds, and CLI probes 15 seconds each. Subprocess groups are terminated if interrupted or timed out. Package-index output is captured and omitted from diagnostics to protect configured credentials.

## Configuration and credentials

Global options precede the command. Base URL precedence is `--base-url`, then `FORGE_BASE_URL`, then `http://127.0.0.1:8000`. Use an absolute HTTP(S) URL with a host and optional port/path. Credentials embedded in a URL, query strings, fragments and control characters are rejected locally.

```sh
cli/.venv/bin/forge --base-url http://127.0.0.1:8000 --json health
cli/.venv/bin/forge --no-color workspace list
```

Read commands for approvals and evidence require `FORGE_APPROVAL_READ_TOKEN`. Exact patch review and decisions require the separate `FORGE_APPROVAL_REVIEW_TOKEN`. Configure them in the local environment through your normal secret-management process. Help/version and unrelated commands do not need these capabilities. Tests generate their own synthetic values.

Tokens follow the backend contract of 32–512 characters from `A-Z a-z 0-9 . _ ~ + / -` with optional trailing `=` padding. The review token does not replace a missing read token. Each authenticated request carries its selected capability in the Authorization bearer header. The client has no global authorization header and rejects bearer use over plaintext HTTP except to IPv4 `127.0.0.0/8`, IPv6 `::1`, or exact hostname `localhost`. It disables environment proxy handling and redirects, uses a five-second HTTP timeout with a two-second connect timeout, and performs no automatic request retries.

## Commands

| Command | Input and behavior |
|---|---|
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
| `forge approval list RUN_UUID` | Read pending approvals. |
| `forge approval get RUN_UUID APPROVAL_UUID` | Read metadata and structural preview. |
| `forge approval review RUN_UUID APPROVAL_UUID` | Read exact patch and checked fingerprint using review capability. |
| `forge approval approve RUN_UUID APPROVAL_UUID --reviewed-patch-sha256 SHA256 --confirm` | Record explicit approval of the reviewed patch. |
| `forge approval reject RUN_UUID APPROVAL_UUID --confirm` | Record explicit rejection. |
| `forge evidence list RUN_UUID` | Read one page with optional `--after-sequence` and `--limit`. |
| `forge evidence get RUN_UUID ATTEMPT_UUID` | Read an attempt with individual check outcomes. |

UUIDs in this reference identify values returned by prior commands. For a fully executable fixture demonstration with generated IDs, use the guarded launcher below. All command-specific help works offline.

## Lifecycle, observation and verification

`Task != AgentRun`. A Task describes the goal and criteria. `run create` creates its initial queued attempt with HTTP 201, or returns the existing initial queued attempt with HTTP 200. It does not start execution. `run start` reports acceptance. Read metadata/events/result to observe execution.

The supported local coordinator binds one trusted workspace and owns execution inside one backend process. The default app has no execution binding and returns 503 for start/cancel. A duplicate start conflicts with 409 and another active run can consume the local capacity. Cancellation persists cancelled metadata and signals active execution. Repeated cancellation of an already cancelled run is safe. Transport failures on mutations leave an uncertain outcome. Read the current state before choosing any further action.

`run watch` defaults to an interval of 2 seconds (1–30), a timeout of 120 seconds (1–600) and cursor 0. Each cycle reads metadata and one page of at most 50 events. It stops after draining a terminal or waiting_approval state. Ctrl-C stops observation with exit 130 and leaves backend cancellation to an explicit command. In JSON mode, interruption or timeout emits no successful snapshot. Human output may already contain observed pages. The polling deadline and cycle count are bounded. An in-flight HTTP call may finish after the polling deadline under the client's HTTP timeout.

Event pages accept limit 1–100 (default 50). Evidence pages accept limit 1–20 (default 20). Both use an integer cursor in 0–2147483647. Sequence gaps are valid. The client checks monotonic progression, next-cursor consistency and has_more semantics. Empty pages keep the input cursor.

`ready_for_verification` describes a preliminary result. Independently recorded evidence supplies pass/fail/inconclusive verdicts and per-check outcomes. The trusted fixture demonstrations call the existing VerificationService separately after execution. Their verified success has `run_status=succeeded` and `verification_verdict=pass`. Production orchestration that automatically drives this verification remains future work.

## Approval safety

Read the exact patch using `approval review` before deciding. The client validates its UTF-8 size (maximum 64 KiB), IDs and domain-separated fingerprint `sha256(b"forge:apply_patch:v1\0" + patch.encode("utf-8"))`. Use that unchanged lowercase fingerprint explicitly with approve. The CLI never performs an implicit review or sends the patch body in a decision POST.

Approval requires `--confirm`. HTTP 200 means the decision was recorded. Subsequent execution and verification have separate outcomes. Stale/conflicting decisions return 409. On decision transport failure, the CLI directs the caller to `forge approval get` and leaves the outcome unknown.

Structural preview states are `live_at_read` and `unavailable`. Preview unavailable carries no implication about patch contents. Exact patch JSON preserves the original string. Human output preserves line boundaries and spaces, escapes terminal controls, and uses Rich Text for untrusted content. The CLI does not save patch files automatically.

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

## Local E2E demonstrations

Previously accepted owner evidence covers CLI-05 TCP/PostgreSQL, bounded paid-provider smoke, active cancellation and Terminal checks, plus CLI-06's four-scenario TCP matrix and interactive approve/reject. The latest owner acceptance for CLI-07 [PR #68](https://github.com/eva-tarasov/myFirstForge/pull/68), reviewed commit `760f8c64d75874a0ddf462299bb20fdbaf1d7b33`, additionally confirms the repeated TCP/PostgreSQL four-scenario matrix PASS and an interactive approve/reject session ending with SESSION_ENDED. Cleanup completed in both runs. Personal evidence/result inspection in Terminal 2 was not established on that interactive run. Paid OpenAI smoke was not repeated in CLI-07. See the release matrix for separate historical and latest owner attribution.

The clearest full fixture path is `run_approval_e2e.zsh tcp`. It exercises Workspace → Task → AgentRun → ToolCall → Approval → Execution → independent Verification → persisted Evidence using real TCP/PostgreSQL and offline LangGraph. Approve writes once, reject/cancel leave bytes unchanged, and a held successful response checks timeout without automatic retry.

Only after explicit owner approval, use the existing disposable container `forge-week4-step06-4d6f653603`, DB `forge_task06_week4_audit_4d6f653603`, port 50088, PostgreSQL 17.11 and revision `1d972cc34fbe`. Require exclusive fixture access, empty business tables, free loopback port 58066 and the existing backend venv at `/Users/dev/Desktop/forge/backend/.venv/bin/python`. The wrapper obtains container credentials in memory and checks the actual DB using SQL. The container bootstrap POSTGRES_DB may differ or be absent.

Inspect help/configuration without Docker or network.

```sh
zsh cli/tests/run_approval_e2e.zsh --help
zsh cli/tests/run_approval_e2e.zsh describe
```

After review, check the actual container/DB and launch one opt-in automatic demonstration.

```sh
zsh cli/tests/run_approval_e2e.zsh check
zsh cli/tests/run_approval_e2e.zsh tcp --approve-db forge_task06_week4_audit_4d6f653603 --confirm-fixture-write
```

Expected automated evidence is outcome PASS, cleanup completed, approve succeeded/pass with one replacement, and reject/cancel/lost-response with zero replacements and no verification PASS. Decisions are recorded once. The report carries actual exits/statuses and per-scenario results. The 240-second scenario budget, 45-second approval phases, four-request per-run offline model cap and scoped filesystem observer remain enforced. See [the harness](tests/demo_approval.py) for exact evidence assertions.

For owner Terminal verification after confirmed cleanup, start Terminal 1 with this command.

```sh
zsh cli/tests/run_approval_e2e.zsh interactive --approve-db forge_task06_week4_audit_4d6f653603 --confirm-fixture-write
```

Wait for READY and copy generated commands one at a time into Terminal 2. Inspect review before the explicitly labelled synthetic-write approval. Complete the decision within 35 seconds of READY. Wait for evidence/result commands after independent verification. Press Return in Terminal 1 to move to the independent reject fixture, then Return again after inspecting its result to stop and clean up. Phase and outer deadlines also terminate the session. Capabilities pass through private local IPC and the CLI child environment. Tokens are absent from the printed commands. Interactive routes are limited to the selected fixture. The final report requires SESSION_ENDED and cleanup completed. Record actual visual observations separately.

The [CLI-05 harness](tests/demo_agent_run.py) retains describe, tcp, active-cancel, interactive and separately gated live modes. Its database identity/empty-table guards, shared advisory lock, protected read-only fixture, subprocess bounds and cleanup remain available. Its historical paid live evidence is retained in the release matrix. Reproduction of a paid test requires a separate owner decision.

If cleanup is unconfirmed, retain the safe fixture identifier and failure code for review before another run. Cleanup deletes only the current fixture's rows and temporary resources. The launchers do not create/drop/migrate databases.

## Security boundaries and limitations

CLI capabilities are scoped local-backend access mechanisms. Production authentication and deployment are future work. Backend grants, policy, transactions and existing fixture filesystem tools enforce the demo's allowed scope. The CLI adds no sandbox isolation. Generic shell execution, MCP, automatic approval and durable restart/resume are outside v0.1. Runtime recovery, production verification orchestration and provider accounting need further work. The owner's learning checkpoint and spent time remain unrecorded.

## Release verification

From the CLI worktree, run focused release tests and the complete offline suite.

```sh
cli/.venv/bin/python -m pytest -q cli/tests/test_release.py
cli/.venv/bin/python -m pytest -q cli/tests
cli/.venv/bin/python -m pip check
```

Then run the clean-install launcher above, record its JSON and inspect Git status/diff. [RELEASE_V0_1.md](RELEASE_V0_1.md) records measured counts, interpreter coverage, accepted historical evidence and remaining owner handoff.
