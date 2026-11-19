import hmac
import re
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from pydantic import SecretStr


def _validate_token(token: SecretStr) -> None:
    secret = token.get_secret_value()
    if not 32 <= len(secret) <= 512 or re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", secret) is None:
        raise ValueError("Invalid local approval access token")


def _matches_token(token: SecretStr, credential: str) -> bool:
    expected = sha256(token.get_secret_value().encode()).digest()
    supplied = sha256(credential.encode()).digest()
    return hmac.compare_digest(expected, supplied)


@dataclass(frozen=True, slots=True)
class LocalApprovalAccessBinding:
    """Backend-configured capability for one workspace in local development."""

    workspace_id: UUID
    token: SecretStr = field(repr=False)
    review_token: SecretStr | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID) or not isinstance(self.token, SecretStr):
            raise ValueError("Invalid local approval access binding")
        _validate_token(self.token)
        if self.review_token is not None:
            if not isinstance(self.review_token, SecretStr):
                raise ValueError("Invalid local approval access binding")
            _validate_token(self.review_token)
            if self.accepts(self.review_token.get_secret_value()):
                raise ValueError("Local approval capabilities must use distinct tokens")

    def accepts(self, credential: str) -> bool:
        return _matches_token(self.token, credential)

    def accepts_review(self, credential: str) -> bool:
        return self.review_token is not None and _matches_token(self.review_token, credential)
