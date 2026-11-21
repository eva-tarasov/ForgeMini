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
