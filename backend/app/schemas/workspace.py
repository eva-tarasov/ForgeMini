import re
from pathlib import Path
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints, field_validator

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}")


class WorkspaceCreate(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    # Absolute path of the project on the machine that runs Forge (local, single-user app).
    source_path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = None
    # Shell command run from the project root; exit code 0 = tests pass. e.g. "python -m unittest".
    test_command: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = None
    # Docker image the agent and the verifier run in. It needs the project's dependencies pre-installed:
    # the sandbox has no network.
    image: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    # Optional setup phase (ADR-004): run ONCE with network before the sandbox, e.g. "pip install -r requirements.txt".
    # Its result is frozen into a derived image; the agent and the verifier still run without network.
    setup_command: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)] | None = None

    @field_validator("source_path")
    @classmethod
    def _existing_directory(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\0" in value:
            raise ValueError("source_path must not contain NUL")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("source_path must be an absolute path")
        if not path.is_dir():
            raise ValueError("source_path must be an existing directory")
        return str(path.resolve())

    @field_validator("test_command", "setup_command")
    @classmethod
    def _no_nul(cls, value: str | None, info) -> str | None:
        if value is not None and "\0" in value:
            raise ValueError(f"{info.field_name} must not contain NUL")
        return value

    @field_validator("image")
    @classmethod
    def _image_ref(cls, value: str | None) -> str | None:
        if value is not None and _IMAGE.fullmatch(value) is None:
            raise ValueError("image is not a valid Docker image reference")
        return value


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    source_path: str | None = None
    test_command: str | None = None
    image: str | None = None
    setup_command: str | None = None
