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

The following examples use an available Forge backend. Replace the sample IDs with the IDs returned by the workspace and task creation commands.

```sh
forge workspace create --name "Проект Forge"
forge --no-color workspace list
forge --json workspace list
forge task create --workspace-id 11111111-1111-4111-8111-111111111111 --goal "Verify the build" --criterion "Tests pass" --criterion "Artifact is produced"
forge task get 22222222-2222-4222-8222-222222222222
```

Repeat `--criterion` to supply multiple acceptance criteria. Task creation accepts an optional `--idempotency-key UUID`, which is forwarded as a header. UUID arguments are validated locally, and request DTOs enforce the backend field limits. Invalid input sends no HTTP requests.

## Output

On a compatible interactive terminal with UTF-8 output, Forge shows workspace lists as Rich tables and individual workspace and task results as panels. IDs are displayed in full, long fields fold, and task criteria are numbered in order. Narrow terminals use labeled text with complete values. The health command prints `status=ok`. An empty workspace list prints `No workspaces found.`.

Use `--no-color` or set `NO_COLOR` to any nonempty value to suppress ANSI color while keeping the terminal layout. Either setting takes precedence over `FORCE_COLOR`. Piped or redirected output, and terminals without UTF encoding, use deterministic `field=value` lines with ASCII escaping and one `acceptance_criteria` line per criterion.

Interactive output preserves ordinary Unicode, including Cyrillic. Forge escapes terminal controls, bidi and other format controls, and displays Rich markup and emoji shortcodes literally. Backslashes in values display doubled so that a literal path containing an escape-like sequence can be distinguished from an escaped control.

`--json` writes only the JSON response object or array to stdout, with UUID strings and JSON-escaped text. An empty workspace list produces `[]`. `--no-color` and terminal capabilities have no effect on the JSON bytes.

Successful results go to stdout. Error diagnostics go to stderr and use local messages without server code, message, details, response body, or full URL.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Invalid command, arguments, or configuration |
| 3 | Transport failure or timeout |
| 4 | Unexpected HTTP status |
| 5 | Invalid successful response |

## Offline tests

From the repository root, run the output tests, command tests, or full CLI suite. The command tests exercise Typer with HTTPX MockTransport and block real sockets. The output tests simulate terminal capabilities and verify redirected output.

```sh
cli/.venv/bin/python -m pytest -q cli/tests/test_output.py
cli/.venv/bin/python -m pytest -q cli/tests/test_commands.py
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

GET operations require HTTP 200 and POST operations require HTTP 201. Other statuses raise `ForgeAPIError` with `status_code`, `expected_status`, `method`, an endpoint template, and `server_code` when the backend error envelope validates. A malformed error envelope leaves `server_code` as `None`. That attribute contains untrusted server data and must be handled explicitly before displaying or logging it. The exception message and representation use only locally generated text and exclude the server code, message, details, body, headers, and full URL.

Connection failures and timeouts raise `ForgeTransportError` with a local `reason`. Invalid JSON and invalid response models at the expected success status raise `ForgeResponseError`. The client performs no terminal output. Calling it after closing raises HTTPX's lifecycle error.

Run the HTTP and DTO tests with `cli/.venv/bin/python -m pytest cli/tests/test_client.py cli/tests/test_dto.py`. They use HTTPX MockTransport and block real sockets.
