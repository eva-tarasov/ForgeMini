"""Local error messages with structured diagnostics for the HTTP boundary."""

from typing import Literal


class ForgeTransportError(Exception):
    def __init__(
        self,
        method: str,
        endpoint: str,
        reason: Literal["connection", "timeout", "transport_failure"],
    ) -> None:
        self.method = method
        self.endpoint = endpoint
        self.reason = reason
        super().__init__(f"Forge transport failure ({reason}) for {method} {endpoint}.")


class ForgeAPIError(Exception):
    """The server_code attribute is untrusted data and is never interpolated."""

    def __init__(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        expected_status: int,
        server_code: str | None,
    ) -> None:
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.expected_status = expected_status
        self.server_code = server_code
        super().__init__(f"Forge returned HTTP {status_code} for {method} {endpoint}.")


class ForgeResponseError(Exception):
    def __init__(self, method: str, endpoint: str, status_code: int) -> None:
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        super().__init__(f"Forge returned an invalid response for {method} {endpoint}.")
