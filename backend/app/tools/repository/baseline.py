from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from itertools import islice
from types import MappingProxyType

from app.permissions.paths import has_protected_component, parse_logical_path
from app.tools.repository.filesystem import MAX_FILE_BYTES, MAX_PATH_DEPTH, RepositoryError


MAX_DIFF_LINES = 1000
MAX_BASELINE_BYTES = 256 * 1024
MAX_REVISION_BYTES = 128


def diff_lines(text: str) -> list[str]:
    # The snapshot comparison uses LF boundaries and keeps any CR in the original data.
    with StringIO(text, newline="\n") as lines:
        result = list(islice(lines, MAX_DIFF_LINES + 1))
    if len(result) > MAX_DIFF_LINES:
        raise RepositoryError("repository_diff_line_budget_exceeded")
    return result


@dataclass(frozen=True, slots=True)
class FixtureBaseline:
    """A bounded immutable text snapshot from trusted fixture setup."""

    revision: str
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not isinstance(self.files, Mapping):
            raise TypeError("Fixture baseline requires a revision and a mapping of files")
        if (
            not 1 <= len(self.revision) <= MAX_REVISION_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in self.revision)
        ):
            raise ValueError("Invalid fixture baseline revision")

        snapshot: dict[str, str] = {}
        try:
            size = len(self.revision.encode("utf-8"))
            if size > MAX_REVISION_BYTES:
                raise ValueError("Invalid fixture baseline revision")
            for path, content in self.files.items():
                components = parse_logical_path(path)
                if not 1 <= len(components) <= MAX_PATH_DEPTH or has_protected_component(components):
                    raise ValueError("Invalid fixture baseline path")
                if not isinstance(content, str):
                    raise TypeError("Fixture baseline contents must be strings")
                if len(content) > MAX_FILE_BYTES or "\0" in content:
                    raise ValueError("Invalid fixture baseline text")
                content_size = len(content.encode("utf-8"))
                if content_size > MAX_FILE_BYTES:
                    raise ValueError("Invalid fixture baseline text")
                size += len(path.encode("utf-8")) + content_size
                if size > MAX_BASELINE_BYTES:
                    raise ValueError("Fixture baseline exceeds its total byte budget")
                diff_lines(content)
                snapshot[path] = content
        except UnicodeEncodeError:
            raise ValueError("Fixture baseline must be UTF-8 text") from None
        except RepositoryError:
            raise ValueError("Fixture baseline exceeds its line budget") from None

        object.__setattr__(self, "files", MappingProxyType(snapshot))
