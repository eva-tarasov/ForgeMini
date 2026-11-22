"""Resolve and validate the Forge backend URL locally."""

import os
import re
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def resolve_base_url(cli_value: str | None = None) -> str:
    """Return the URL from the CLI flag, environment, or default, in that order."""
    value = cli_value
    if value is None:
        value = os.getenv("FORGE_BASE_URL", DEFAULT_BASE_URL)

    error = "Expected a valid absolute HTTP(S) URL without credentials, a query, or a fragment."

    # urlsplit strips certain whitespace and control characters before parsing.
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(error)
    if any(char in value for char in "\\?#"):
        raise ValueError(error)

    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError(error) from None

    if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
        raise ValueError(error)
    # Reject malformed authorities that urlsplit can parse, such as [::1]suffix.
    authority = r"(?:\[[^\[\]]+\]|[^:\[\]%]+)(?::[0-9]+)?"
    if port == 0 or not re.fullmatch(authority, url.netloc):
        raise ValueError(error)

    return value
