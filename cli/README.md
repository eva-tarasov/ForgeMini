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
