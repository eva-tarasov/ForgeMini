# Forge CLI

Install with Python 3.11 or later. From the repository root, create a dedicated environment and install the CLI with its test dependencies.

```sh
python3.13 -m venv cli/.venv
cli/.venv/bin/python -m pip install -e './cli[dev]'
cli/.venv/bin/forge --help
cli/.venv/bin/forge --version
cli/.venv/bin/python -m pytest cli/tests
```

`forge` shows help with a valid configuration. `forge --help` and `forge --version` work with the backend offline, including when the configured URL is invalid. The version command prints `0.1.0`.

The CLI selects `--base-url` first, then `FORGE_BASE_URL`, then `http://127.0.0.1:8000`. Use an absolute HTTP or HTTPS URL with a host and optional port and path. Empty values, embedded credentials, queries, and fragments are rejected. The CLI validates the URL locally without opening a connection.

```sh
FORGE_BASE_URL=https://forge.example.invalid cli/.venv/bin/forge
cli/.venv/bin/forge --base-url http://127.0.0.1:9000
```

## Commands

Global options go before the command. Use `forge --base-url http://127.0.0.1:8000 health` to select the backend explicitly.

| Command | Required input |
| --- | --- |
| `forge health` | None |
| `forge workspace create` | `--name NAME` |
| `forge workspace list` | None |
| `forge task create` | `--workspace-id UUID --goal TEXT --criterion TEXT` |
| `forge task get UUID` | Task UUID |
| `forge run create --task-id UUID` | Task UUID |
| `forge run get UUID` | AgentRun UUID |
| `forge run start UUID` | AgentRun UUID |
| `forge run cancel UUID` | AgentRun UUID |
| `forge run events UUID` | AgentRun UUID |
| `forge run result UUID` | AgentRun UUID |
| `forge run watch UUID` | AgentRun UUID |

The following examples use an available Forge backend. Replace the sample IDs with the IDs returned by the workspace and task creation commands.

```sh
forge workspace create --name "Проект Forge"
forge --no-color workspace list
forge --json workspace list
forge task create --workspace-id 11111111-1111-4111-8111-111111111111 --goal "Verify the build" --criterion "Tests pass" --criterion "Artifact is produced"
forge task get 22222222-2222-4222-8222-222222222222
```

Repeat `--criterion` to supply multiple acceptance criteria. Task creation accepts an optional `--idempotency-key UUID`, which is forwarded as a header. UUID arguments are validated locally, and request DTOs enforce the backend field limits. Invalid input sends no HTTP requests.

## AgentRun lifecycle

`run create` creates the initial queued attempt with HTTP 201. Repeating creation while that attempt is the only run and is still queued returns the same run with HTTP 200. `run get` reads current metadata. `run start` sends one request and reports acceptance from HTTP 202. Use metadata or result reads to determine the actual state. A duplicate start that was already accepted or started returns HTTP 409.

`run cancel` sends one request and expects HTTP 200 with matching cancelled metadata. An already cancelled run can return that same outcome. A transport failure on start or cancel leaves the effect uncertain. Inspect `run get UUID` before deciding what to do next. Both commands leave retries to the caller.

The current execution coordinator requires a trusted binding and runs in one local backend process. The default app can serve read routes and returns HTTP 503 for start or cancel without that binding. In-process execution has no durable restart or resume guarantee.

`run events` reads one page with `--after-sequence` (default 0, range 0–2147483647) and `--limit` (default 50, range 1–100). Use the returned `next_after_sequence` for a later query when `has_more` is true. Sequences may have gaps. An empty page describes the current read and the cursor stays unchanged.

`run result` displays an AgentRun snapshot. `ready_for_verification` is a preliminary result. A verified success has `run_status=succeeded` and `verification_verdict=pass`. Known token and cost figures cover the recorded subset when the corresponding completeness flag is false. An unknown value is distinct from a recorded zero. Cost is a decimal string in JSON.

## Bounded observation

`run watch UUID` reads metadata and one page of up to 50 events per cycle. `--interval` defaults to 2 seconds and accepts integers from 1 to 30. `--timeout` defaults to 120 seconds and accepts integers from 1 to 600. `--after-sequence` defaults to 0 and accepts 0 through 2147483647. Use it to start after an explicitly chosen cursor.

Watch continues through `queued`, `running` and `verifying`, even when a page is empty. Seeing a terminal state or `waiting_approval` starts a bounded drain of currently available pages. If approval proceeds during the drain, the latest observed state may differ from the reason observation stopped. Later instrumentation or cleanup can add events after the last page. Run result is available through a separate command.

The deadline uses monotonic time and is checked before and after reads. Watch sleeps no longer than the remaining budget or configured interval. It makes at most two GETs per cycle across at most `ceil(timeout / interval)` cycles. An in-flight synchronous HTTP request can finish after the cooperative deadline. HTTPX connect, read, write and pool timeouts apply to individual phases. Ctrl-C ends the local observation with exit 130. The deadline exits with code 6. The run remains under backend control.

Human output reports status changes, newly observed events and a final summary on completion, timeout or interruption. Observations already printed can remain on stdout after a later read failure. In JSON mode stdout is empty on timeout, interruption or HTTP failure. A successful observation prints the following fields in one JSON document.

| Field | Meaning |
| --- | --- |
| `run_id` | Requested AgentRun UUID |
| `last_observed_status` | Latest AgentRun status read |
| `events` | Public events observed during this invocation, in server order |
| `next_after_sequence` | Cursor after the last delivered page |
| `stop_reason` | `terminal` or `waiting_approval` |

Use an actual AgentRun UUID in place of the sample ID.

```sh
forge --json run events 33333333-3333-4333-8333-333333333333 --limit 20
forge --no-color run watch 33333333-3333-4333-8333-333333333333 --interval 2 --timeout 60
forge --json run result 33333333-3333-4333-8333-333333333333
```

## Output

On a compatible interactive terminal with UTF-8 output, Forge shows workspace lists as Rich tables and individual workspace and task results as panels. IDs are displayed in full, long fields fold, and task criteria are numbered in order. Narrow terminals use labeled text with complete values. The health command prints `status=ok`. An empty workspace list prints `No workspaces found.`.

Use `--no-color` or set `NO_COLOR` to any nonempty value to suppress ANSI color while keeping the terminal layout. Either setting takes precedence over `FORCE_COLOR`. Piped or redirected output, and terminals without UTF encoding, use deterministic ASCII-escaped text. Event, result and watch output retain labeled fields and use the same event timeline text.

Interactive output preserves ordinary Unicode, including Cyrillic. Forge escapes terminal controls, bidi and other format controls, and displays Rich markup and emoji shortcodes literally. Backslashes in values display doubled so that a literal path containing an escape-like sequence can be distinguished from an escaped control.

`--json` writes only a JSON document to stdout, with UUID strings and JSON-escaped text. An empty workspace list produces `[]`. Watch uses the documented observation object. `--no-color` and terminal capabilities have no effect on the JSON bytes.

Successful results go to stdout. Error diagnostics go to stderr and use local messages without server code, message, details, response body, or full URL.

| Exit code | Meaning |
| --- | --- |
| 0 | Command succeeded, including watch observing a failed, cancelled or waiting AgentRun |
| 2 | Invalid command, arguments, or configuration |
| 3 | Transport failure or timeout |
| 4 | Unexpected HTTP status |
| 5 | Invalid successful response |
| 6 | Watch observation deadline expired |
| 130 | Watch observation interrupted by Ctrl-C |

## Offline tests

From the repository root, run the output tests, command tests, or full CLI suite. The command tests exercise Typer with HTTPX MockTransport and block real sockets. The output tests simulate terminal capabilities and verify redirected output.

```sh
cli/.venv/bin/python -m pytest -q cli/tests/test_output.py
cli/.venv/bin/python -m pytest -q cli/tests/test_commands.py
cli/.venv/bin/python -m pytest -q cli/tests/test_polling.py
cli/.venv/bin/python -m pytest -q cli/tests
```

## Python HTTP client

Use the synchronous client as a context manager. It owns its HTTPX client and any supplied transport, and closes both when the context ends, including after an exception.

```python
from forge_cli.client import ForgeAPIClient
from forge_cli.dto import WorkspaceCreate

with ForgeAPIClient("http://127.0.0.1:8000") as client:
    health = client.get_health()
    workspaces = client.list_workspaces()
    workspace = client.create_workspace(WorkspaceCreate(name="Forge"))
```

`create_task(TaskCreate(...), idempotency_key=UUID(...))` sends the optional UUID as the `Idempotency-Key` header. `get_task(UUID(...))` retrieves a task. Request models apply the backend's field limits and whitespace normalization. UUID fields remain UUID objects in response models.

Pass the base URL explicitly. The existing URL validator checks it locally. An optional path prefix is preserved. Timeouts are 2 seconds for connecting and 5 seconds each for reading, writing, and waiting for a pooled connection. Redirect following, environment proxy/credentials discovery, and automatic retries are disabled. TLS certificate verification uses HTTPX defaults.

GET operations require HTTP 200. Workspace and Task creation require HTTP 201. AgentRun creation accepts HTTP 201 or 200, start requires HTTP 202 and cancel requires HTTP 200. Other statuses raise `ForgeAPIError` with `status_code`, `expected_status`, `method`, an endpoint template, and `server_code` when the backend error envelope validates. A malformed error envelope leaves `server_code` as `None`. That attribute contains untrusted server data and must be handled explicitly before displaying or logging it. The exception message and representation use only locally generated text and exclude the server code, message, details, body, headers, and full URL.

Connection failures and timeouts raise `ForgeTransportError` with a local `reason`. Invalid JSON and invalid response models at the expected success status raise `ForgeResponseError`. The client performs no terminal output. Calling it after closing raises HTTPX's lifecycle error.

Run the HTTP and DTO tests with `cli/.venv/bin/python -m pytest cli/tests/test_client.py cli/tests/test_dto.py`. They use HTTPX MockTransport and block real sockets.

## CLI-05 demo acceptance

The opt-in [demo launcher](tests/demo_agent_run.py) starts Uvicorn on `127.0.0.1:58066` and runs the installed Forge CLI in separate processes. The owner supplied the following results for baseline `1140d7a06bca196caf49c6cb90dd9d2d6ba194e3` in [PR #66](https://github.com/eva-tarasov/myFirstForge/pull/66).

| Check | Evidence |
|---|---|
| Offline CLI regression | 460 tests passed. |
| Real TCP, PostgreSQL and deterministic provider | PASS. Fixture unchanged and cleanup completed. |
| Real TCP, PostgreSQL and live OpenAI | PASS. Three model requests, 1053 tokens, `usage_complete=true`, cost unknown. Tool audit `read_file / succeeded`, independent verification `pass`, final run `succeeded`. Fixture unchanged and cleanup completed. |
| Active cancellation over real TCP | Prepared for owner review. Execution pending. |
| Manual verification in two macOS terminals | Prepared for owner review. Execution pending. |

The Week 4 backend ASGI checks exercise in-process HTTP routing. The demo evidence above covers separate Forge processes and a loopback TCP listener. The TCP and live success scenarios both use trusted independent fixture verification invoked by the launcher after the preliminary result is committed. Production automation that drives an AgentRun from `verifying` to `succeeded` is outside this demo.

### Review the environment without opening a connection

From the CLI worktree root, inspect the fixed environment and budgets.

```sh
cd /Users/dev/Desktop/myFirstForge-week4cli
cli/.venv/bin/python cli/tests/demo_agent_run.py describe
cli/.venv/bin/python cli/tests/demo_agent_run.py --help
```

The launcher requires exclusive use of the empty disposable DB `forge_task06_week4_audit_4d6f653603` at `127.0.0.1:50088`, PostgreSQL 17.11, `READ COMMITTED`, and Alembic revision `1d972cc34fbe`. It confirms identity with the Week 4 read-only guard, takes a transaction-scoped advisory lock against another demo launcher, and refuses pre-existing fixture business rows. The owner must confirm exclusivity from other processes. It also requires a free IPv4 loopback port `58066`, the existing backend venv, and the editable CLI venv from this worktree. The default backend app has no execution binding and start/cancel return HTTP 503. The launcher first uses this app to create a disposable workspace through the CLI, then uses a fresh app on the same port with a trusted binding to the returned Workspace UUID.

The execution fixture is a fresh temporary directory containing only the synthetic `sample.txt` from the Week 4 smoke, opened through `REPOSITORY_READ` and `PathScope("sample.txt")`. The launcher checks the directory and bytes after the run. Forge processes receive only allowlisted terminal and locale settings and `PATH`. Database and provider credentials stay in the launcher environment. The automated scenarios and interactive setup each have a 90-second budget, plus bounded preflight, shutdown and cleanup. Server startup and DB transactions each have a 5-second budget, server shutdown has an 8-second outer wait, ordinary CLI subprocesses have 10 seconds, and the verified watch uses a 15-second observation budget with a 25-second process wait.

### TCP integration with deterministic provider replies

After the owner approves the isolation above, supply fixture-only `POSTGRES_USER` and `POSTGRES_PASSWORD` through the environment. The launcher does not load a root .env file. Run from this worktree root.

```sh
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=50088 POSTGRES_DB=forge_task06_week4_audit_4d6f653603
export FORGE_REPOSITORY_AGENT_TEST_DB=forge_task06_week4_audit_4d6f653603
PYTHONPATH=backend /Users/dev/Desktop/forge/backend/.venv/bin/python cli/tests/demo_agent_run.py tcp --approve-db forge_task06_week4_audit_4d6f653603
```

PostgreSQL, FastAPI and the CLI HTTP connection are real. Only provider replies are deterministic, through the existing Week 4 `offline_reply` behind HTTPX MockTransport. The launcher runs `health`, `workspace create`, `task create` and every AgentRun command. It obtains IDs from CLI JSON and verifies queued replay with the same ID, start acceptance and a duplicate conflict, a standalone metadata read, event cursor progression, watch, and preliminary and verified result reads. Separate queued runs exercise cancellation, repeated cancellation and capacity. The backend HTTP coordinator does not perform independent verification. The launcher calls trusted `VerificationService.verify(..., isolated_fixture=True)` once with a file-digest plan after observing the committed preliminary result. It checks the actual verdict, completed read audit, verification event and terminal snapshot.

The negative matrix covers real connection refusal (exit 3), invalid UUID/limit/timeout (exit 2 with zero HTTP requests), real 404/409/429/503 (exit 4), and direct backend validation HTTP 422. The CLI checks input locally, so the 422 probe uses a separate HTTP request. Deliberately faulted responses for two synthetic GET IDs exercise HTTP 500 (exit 4) and malformed HTTP 200 (exit 5) over the real listener, without corrupting DB rows. A separate queued run checks watch deadline and SIGINT (exits 6 and 130) with JSON stdout empty and GET-only traffic. Backend faults and provider simulation are distinct from the normal real HTTP/DB evidence.

### Active cancellation over TCP with an offline provider

After approval, use the same dedicated database and fixture-only `POSTGRES_USER` and `POSTGRES_PASSWORD` in the launcher environment. Run this scenario separately from other demo sessions.

```sh
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=50088 POSTGRES_DB=forge_task06_week4_audit_4d6f653603
export FORGE_REPOSITORY_AGENT_TEST_DB=forge_task06_week4_audit_4d6f653603
PYTHONPATH=backend /Users/dev/Desktop/forge/backend/.venv/bin/python cli/tests/demo_agent_run.py active-cancel --approve-db forge_task06_week4_audit_4d6f653603
```

Forge creates the workspace, tasks and runs over TCP. The first model request reaches an `asyncio.Event` barrier in HTTPX MockTransport and waits without a model reply. After Forge reads committed `running` metadata, the launcher sends SIGINT to `run watch` after the observed metadata and events GETs. It checks exit 130, precisely those two successful GETs, committed `running` after SIGINT, and the original backend task still registered and active.

Explicit `run cancel` must return HTTP 200 with `cancelled` metadata. The launcher requires the blocked MockTransport handler to observe `CancelledError` and the original coordinator task to settle with its execution slot and active-registry entry cleared. Barrier entry and teardown each use a 5-second wait. A second fixture run must receive HTTP 429 while the first is active. After cancellation, the same probe must receive HTTP 202 and reach its separate runner barrier before any model call. The launcher cancels and settles the probe, too.

A new metadata read, full event page and result read must show the first run stayed cancelled, with exactly `queued → running → cancelled` and no preliminary result, execution failure, verification, or tool audit. Repeated cancel must return the same metadata. A successful report includes `model_requests: 1`, `execution_cleanup: "observed"`, `execution_capacity_released: true`, `outcome: "PASS"` and `cleanup: "completed"`.

### Manual verification in two macOS terminals

In Terminal 1, use the same isolated database credentials. Launch from the worktree root with stdin and stdout attached to your terminal.

```sh
cd /Users/dev/Desktop/myFirstForge-week4cli
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=50088 POSTGRES_DB=forge_task06_week4_audit_4d6f653603
export FORGE_REPOSITORY_AGENT_TEST_DB=forge_task06_week4_audit_4d6f653603
PYTHONPATH=backend /Users/dev/Desktop/forge/backend/.venv/bin/python cli/tests/demo_agent_run.py interactive --approve-db forge_task06_week4_audit_4d6f653603
```

Wait for `READY`. The launcher prints exact Terminal 2 commands with the actual Workspace, Task and AgentRun UUIDs. Copy them one at a time into Terminal 2. The `terminal-cli` helper hands the existing TTY directly to installed Forge with the fixed loopback base URL and the same environment allowlist. During the owner session, the server admits only workspace listing, reads for the printed Task and AgentRun, and start/cancel for that AgentRun.

1. Inspect `workspace list`, `task get` and `run get` while the run is queued. Look for the workspace table, labelled fields and complete IDs. Narrow Terminal 2 and repeat `run get`. Check that wrapped IDs can still be recovered in full, then restore the width. Compare the printed `--json` and `--no-color` commands with the default output on a color-capable terminal.
2. Run the printed `run start` and `run watch` commands. Once the watch displays `running`, press Ctrl-C in Terminal 2 and immediately execute the next `run get`. It should still report `running`. The backend's 60-second execution deadline starts at `run start`. The provider stays at the offline barrier during this check.
3. Run the printed `run cancel`, `run events`, `run result` and final `run watch`. Check for `cancelled`, ordered transitions and a terminal watch summary. Save the visual observations with the final launcher report.
4. Press Return in Terminal 1 after the checks. The launcher stops automatically after 180 seconds if left idle or open. It emits `outcome: "SESSION_ENDED"`, `manual_verification: "owner_pending"` and `cleanup: "completed"` when the session ends and cleanup is confirmed. Use the actual Terminal 2 observations to record manual acceptance.

### Separate bounded live OpenAI smoke (baseline evidence)

Approve this run separately. Use the same guarded database in a separate invocation after TCP cleanup. Supply a dedicated OpenAI project key as `OPENAI_API_KEY` through the environment, and verify that the project permits `gpt-6-luna`. The backend uses the existing Week 4 guard for Chat Completions at `https://api.openai.com/v1`. Keep external tracing disabled. No credentials belong in commands, reports or the repository.

```sh
export FORGE_WEEK4_LIVE_SMOKE=1 FORGE_MODEL_NAME=gpt-6-luna
PYTHONPATH=backend /Users/dev/Desktop/forge/backend/.venv/bin/python cli/tests/demo_agent_run.py live --approve-db forge_task06_week4_audit_4d6f653603 --approve-live-model gpt-6-luna
```

The paid run exercises one synthetic read-only AgentRun. The guard caps provider requests at **3**, serialized request bodies at **16 KiB each**, and completion tokens at **512 each** (at most 1536 requested completion tokens across the run). The model timeout is 10 seconds with retries disabled, and the outer scenario budget is 90 seconds. Input tokens and billed cost depend on the actual requests and provider accounting. These technical bounds are not a dollar cap. The launcher fails if the run does not commit a preliminary result, perform one completed `read_file`, and pass separate trusted verification. Provider refusal, unavailable model access and incompatible output remain possible. The live mode omits the offline failure matrix and waits for the actual provider and verification outcomes.

### Cleanup and evidence criteria

On completion or failure, the launcher releases the offline reply gate, terminates any outstanding CLI subprocess, stops Uvicorn and rechecks the DB guard before deleting only the smoke workspace and its dependent rows. It confirms the DB returns to empty, disposes the engine and transport, and removes the temporary repository. An interrupted or unsuccessful cleanup is reported as unconfirmed with the synthetic workspace marker for targeted recovery. No database drop, migration or automatic rerun is part of this harness.

Automated runs emit actual command exits, HTTP statuses, model request count and mode-specific results. The interactive report captures admitted HTTP traffic and cleanup. Keep each report with its PR commit and record the terminal observations separately. Final CLI-05 acceptance still requires a reviewed active-cancellation run and the owner's actual Terminal 2 observations.

## CLI-06A approval and evidence reads

The CLI exposes five GET operations using the backend's existing read APIs.

| Command | HTTP route | Capability environment variable |
|---|---|---|
| `forge approval list RUN_UUID` | `/agent-runs/{run_id}/approvals` | `FORGE_APPROVAL_READ_TOKEN` |
| `forge approval get RUN_UUID APPROVAL_UUID` | `/agent-runs/{run_id}/approvals/{approval_id}` | `FORGE_APPROVAL_READ_TOKEN` |
| `forge approval review RUN_UUID APPROVAL_UUID` | `/agent-runs/{run_id}/approvals/{approval_id}/review` | `FORGE_APPROVAL_REVIEW_TOKEN` |
| `forge evidence list RUN_UUID --after-sequence N --limit N` | `/agent-runs/{run_id}/evidence` | `FORGE_APPROVAL_READ_TOKEN` |
| `forge evidence get RUN_UUID ATTEMPT_UUID` | `/agent-runs/{run_id}/evidence/{attempt_id}` | `FORGE_APPROVAL_READ_TOKEN` |

The required capability is checked locally on each request. Tokens must contain 32–512 characters from the backend's bearer alphabet, with padding only at the end. The read and review environment variables are independent. The CLI sends the selected token through the request's Authorization header. Unrelated commands keep their existing HTTP behavior. Help and version work without credentials.

Bearer requests require HTTPS or HTTP to an IPv4 address in `127.0.0.0/8`, IPv6 `::1`, or the hostname `localhost`. HTTP lookalike names, integer host encodings and other destinations are rejected locally. The client retains `trust_env=False`, disabled redirects and its existing timeouts. Configure real capabilities through your local environment only when exercising an authorized backend. Routine tests supply synthetic credentials themselves.

Response DTOs reject unknown fields, invalid types and inconsistent approval decision timestamps. The pending list is bounded to 32 items. Structural previews distinguish `live_at_read` from `unavailable`, always exclude content, and enforce file, line and hunk limits. `Preview unavailable` indicates unavailable structural information.

Exact reviews preserve raw patch data in JSON. The CLI verifies the lowercase fingerprint using `sha256(b"forge:apply_patch:v1\0" + patch.encode("utf-8"))` and enforces the 64 KiB UTF-8 limit. Human output preserves patch line boundaries and spaces, escapes terminal controls, and renders untrusted content as Rich Text. Wide patch lines use terminal soft wrapping. JSON consumers can recover the original patch exactly. Patch content is excluded from DTO repr and is never written to a file automatically.

Evidence pages default to cursor 0 and limit 20. Limits are 1–20. Sequences must increase but may contain gaps. The next cursor must equal the last returned sequence, or the input cursor for an empty page. A page advertising more data must fill the requested limit and leave room for a greater sequence. Evidence detail validates its attempt identity and check count. Its backend response contains no run ID, so run association is expressed by the requested URL. Empty pages display their actual empty state.

Global `--json` and `--no-color` apply to both groups. Human results use labelled Rich panels in interactive terminals and deterministic escaped lines when piped. Narrow layouts retain complete identifiers and fields. Safe errors use the existing exit categories, with local capability configuration failures using exit 2, transport failures 3, HTTP failures 4 and malformed successful responses 5.

### Offline owner verification

Use the existing editable CLI installation and its virtual environment in `/Users/dev/Desktop/myFirstForge-week4cli` on branch `feat/week4cli-approvals`. Backend, Docker, PostgreSQL, OpenAI and external credentials are unnecessary. The focused tests use HTTPX MockTransport and block real sockets.

Check the branch and working tree.

```sh
cd /Users/dev/Desktop/myFirstForge-week4cli
git branch --show-current
git status --short
```

Expect the branch above and the scoped CLI changes.

Run the focused checks and one full regression.

```sh
cli/.venv/bin/python -m pytest -q cli/tests/test_approval_evidence.py
cli/.venv/bin/python -m pytest -q cli/tests
```

Measured on this CLI-06A implementation, the focused suite passed 204 tests and the full suite passed 679 tests, with zero failures or skips. Record your own summaries, including any differences. For a failing focused case, use verbose test names with traceback and captured payload output suppressed.

```sh
cli/.venv/bin/python -m pytest cli/tests/test_approval_evidence.py -v -x --tb=no --show-capture=no
cli/.venv/bin/python -m pip check
```

Expect the failing test name and summary when diagnosing a failure. Dependency validation should report `No broken requirements found`. Keep credentials and private patches out of reviewer diagnostics.

Verify both command groups offline and inspect the diff.

```sh
cli/.venv/bin/forge approval --help
cli/.venv/bin/forge evidence --help
git diff --check
git diff --stat
```

Expect approval list/get/review and evidence list/get, successful help exits, and an empty whitespace-check result. Tests create no persistent external resources. Normal pytest caches may remain in the worktree. Send the reviewer both pytest summaries, help verification, dependency result, Git status/diff summary, failures/skips and known limitations.

CLI-06B still requires real TCP/PostgreSQL approval and evidence lifecycle evidence, including approve/reject and exact-review integrity during a live pending operation. CLI-06A provides offline read-contract verification.

## CLI-06B decisions and approval acceptance

Approve and reject record a decision on an existing active approval. Both send one POST to `/agent-runs/{run_id}/approvals/{approval_id}/decision` using `FORGE_APPROVAL_REVIEW_TOKEN`. Approve requires an explicitly supplied lowercase fingerprint in `--reviewed-patch-sha256` and `--confirm`. Reject requires `--confirm`. The CLI validates input locally, sends only the decision DTO, and validates the returned IDs, decision and strict JSON `recorded: true`. HTTP 200 confirms that the decision was recorded. Filesystem execution and verification have their own subsequent outcomes.

A transport failure returns exit 3 and describes the decision outcome as unknown. Read the approval state through `forge approval get` before choosing any further action. POST requests are never retried automatically. Missing confirmation, invalid fingerprint or missing review capability return exit 2 without HTTP. Backend failures return exit 4. Invalid successful responses return exit 5. JSON and global no-color behavior also apply to decisions.

### Offline verification

The existing CLI environment is sufficient. Tests provide synthetic tokens and MockTransport, with IP networking blocked. Backend, Docker, PostgreSQL and OpenAI are unnecessary for this part.

Check the existing worktree and baseline.

```sh
cd /Users/dev/Desktop/myFirstForge-week4cli
git branch --show-current
git rev-parse HEAD
git status --short
```

Expect `feat/week4cli-approvals`, baseline `09a47be42c6a89ec11d308a0ef203990440b577f`, and the scoped CLI-06B changes.

Run the focused suites and one complete regression.

```sh
cli/.venv/bin/python -m pytest -q cli/tests/test_approval_decisions.py
cli/.venv/bin/python -m pytest -q cli/tests/test_demo_approval.py
cli/.venv/bin/python -m pytest -q cli/tests
```

Measured offline results are 52 passed for decisions, 22 passed for harness guards and 753 passed for the full CLI suite, with zero failures or skips. Save your measured summaries. The first suite covers the two production commands. The second covers launcher guards, scope restrictions and bounded task observation with external execution blocked.

Check help and the dependency environment.

```sh
cli/.venv/bin/forge approval approve --help
cli/.venv/bin/forge approval reject --help
cli/.venv/bin/python -m pip check
```

Expect both help commands to exit 0, documented confirmation/fingerprint options, and `No broken requirements found`.

Check the launcher without Docker or credentials, then inspect whitespace and diff.

```sh
zsh cli/tests/run_approval_e2e.zsh --help
zsh cli/tests/run_approval_e2e.zsh describe
git diff --check
git diff --stat 09a47be
```

Expect help/configuration only and a clean whitespace check. New untracked test/helper files appear in Git status separately from tracked diff statistics. For failure diagnostics use verbose test names with payload capture and traceback suppressed.

```sh
cli/.venv/bin/python -m pytest cli/tests/test_approval_decisions.py cli/tests/test_demo_approval.py -v -x --tb=no --show-capture=no
git status --short
```

These tests create no database, container or provider resources. Ordinary Python/pytest caches may remain. Send the reviewer the test summaries, failures/skips, help results, dependency result and Git status/diff. Include the integration execution status.

### Real TCP acceptance after owner review

Execution of this new integration harness is pending. The owner must first review it and explicitly authorize the fixture writes. Use the same worktree and installed CLI, Docker Desktop, the existing backend virtual environment at `/Users/dev/Desktop/forge/backend/.venv/bin/python`, a free loopback port 58066, and exclusive use of the existing disposable PostgreSQL container `forge-week4-step06-4d6f653603`. It must publish port 50088, identify its DB as `forge_task06_week4_audit_4d6f653603`, run PostgreSQL 17.11, and have Alembic revision `1d972cc34fbe`. Business tables must be empty. The wrapper reads the container's existing credentials in memory. It requires its POSTGRES_DB, POSTGRES_USER and POSTGRES_PASSWORD configuration. Credentials never need to be copied or saved by the owner.

The wrapper performs Docker and read-only database identity checks. The harness repeats database identity checks, acquires the same advisory lock as the CLI-05 demo and checks empty business tables. It binds the loopback socket before creating fixture records. The launcher removes provider/tracing credentials and disables tracing. Each Forge process receives only terminal/locale settings and synthetic read/review capabilities. All model requests use HTTPX MockTransport and a closed synchronous transport.

After review, check the actual Docker/DB environment and review the fixed budget.

```sh
zsh cli/tests/run_approval_e2e.zsh check
zsh cli/tests/run_approval_e2e.zsh describe
```

Expect `environment: ready`, the exact database/container names, a 240-second scenario budget, 45-second approval phases, at most four offline model requests per run, 16 KiB request bodies and 512 completion tokens per request. The automatic scenario uses at most ten simulated requests across its four runs. Preflight operations are individually bounded, server shutdown waits at most eight seconds, and cleanup uses bounded database transactions. No migration, database creation or automatic rerun occurs.

Run the automatic acceptance with one invocation.

```sh
zsh cli/tests/run_approval_e2e.zsh tcp --approve-db forge_task06_week4_audit_4d6f653603 --confirm-fixture-write
```

The automatic scenario uses the real backend approval gate and offline LangGraph. Approve must observe committed waiting_approval, unchanged bytes before the decision, discovery, structural preview and exact review. A stale fingerprint must return 409 and leave the approval pending. Explicit approval must record once, resume the same execution, and perform exactly one scoped replacement. The trusted harness then invokes the existing VerificationService independently and reads persisted PASS evidence and the succeeded result through the CLI. Reject and cancel use separate runs, workspaces and temporary fixtures. Both must leave bytes unchanged and record one rejected approval. Reject must produce failed/graph_execution_failed with an approval_rejected tool audit. Cancel must produce cancelled with a cancelled tool audit. Repeated decisions must conflict. Missing/wrong capabilities and foreign workspace/run/approval identities are checked through actual HTTP or local input rejection as appropriate.

The lost-response fixture records a real rejection and holds the successful HTTP response behind an event until the CLI times out. The CLI must exit 3, send exactly one POST and reconcile by reading the persisted rejected state. Its report must have `ambiguous_post_no_retry: true`.

PASS requires exit 0, `outcome: "PASS"`, `cleanup: "completed"` and these actual scenario results in the final JSON report.

| Path | Run status | Decision | Recorded events | Replacements | Offline model requests | Verification |
|---|---|---|---|---|---|---|
| approve | succeeded | approved | 1 | 1 | 4 | pass |
| reject | failed | rejected | 1 | 0 | 2 | null |
| cancel | cancelled | rejected | 1 | 0 | 2 | null |
| lost-response | failed | rejected | 1 | 0 | 2 | null |

Each scenario must additionally report `execution_cleanup: "observed"` and `outside_scope_unchanged: true`. Approve must have `evidence_checked: true`. The report includes actual CLI exits and HTTP statuses, plus a generated safe fixture identifier. Any unmet assertion, nonzero exit, FAILED outcome or unconfirmed cleanup fails acceptance.

### Interactive acceptance in two terminals

After successful automatic cleanup, start this single command in Terminal 1.

```sh
zsh cli/tests/run_approval_e2e.zsh interactive --approve-db forge_task06_week4_audit_4d6f653603 --confirm-fixture-write
```

Wait for READY. Terminal 1 prints complete commands with actual run/approval IDs and fingerprint. Copy them one at a time into Terminal 2. Read the list, structural preview and exact patch first. The printed approve command is explicitly labelled as authorization to change the temporary synthetic sample.txt. Complete the decision within 35 seconds of READY to leave time within the gate's 45-second deadline. Then wait for the launcher to print the evidence list/get and run result commands after independent verification. Inspect them in Terminal 2, including no-color, JSON and a narrowed window as desired. Global flags go after the helper's `--` and before the Forge command.

Press Return in Terminal 1 to begin the independent reject fixture. At its READY, copy the new review and reject commands. Inspect the final rejected result, then press Return in Terminal 1 to shut down and clean up. Ctrl-C in Terminal 1 also invokes bounded shutdown and fixture cleanup. Missing an interactive phase deadline ends the session with failure and cleanup reporting.

The helper retrieves synthetic capabilities through a Unix-domain socket inside a private 0700 temporary directory. Tokens travel in memory through local IPC and the Forge child environment. The printed socket path contains no credential. No token or patch file is created. Interactive middleware permits only the current fixture's displayed routes. Closing each phase closes the capability channel.

An interactive session that completes its assertions reports `outcome: "SESSION_ENDED"`, `manual_verification: "owner_pending"` and `cleanup: "completed"`. Save the actual visual observations separately. The automatic checks and the terminal session both require confirmed backend task completion and cleanup. Only current fixture records are deleted, followed by a fresh empty-table check. The temporary repository and local socket are removed when the launcher exits.

If execution fails, save its safe failure code, fixture identifier and cleanup status. Use the read-only wrapper check above for environment diagnostics. If cleanup is unconfirmed, stop further acceptance runs and review that specific fixture's remaining resources with the owner. Never drop/recreate the database or use a broad deletion command. Send the reviewer the final JSON reports, terminal observations, Git SHA/status, offline summaries and any limitations. Until the owner runs and reviews these scenarios, real CLI-06 integration acceptance remains pending.
