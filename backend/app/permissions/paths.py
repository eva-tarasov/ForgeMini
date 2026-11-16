MAX_LOGICAL_PATH_LENGTH = 4096


def parse_logical_path(path: str) -> tuple[str, ...]:
    """Validate a canonical logical path and return its components."""
    if not isinstance(path, str):
        raise TypeError("Logical path must be a string")
    if (
        not 1 <= len(path) <= MAX_LOGICAL_PATH_LENGTH
        or path.startswith(("/", "~"))
        or "\\" in path
        or ":" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("Invalid logical path")
    if path == ".":
        return ()

    components = tuple(path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("Invalid logical path")
    return components


def has_protected_component(components: tuple[str, ...]) -> bool:
    for component in components:
        folded = component.casefold()
        if folded in {".git", ".env"} or folded.startswith(".env."):
            return True
    return False


def matches_scope(
    path: tuple[str, ...], scope: tuple[str, ...], *, recursive: bool
) -> bool:
    if recursive:
        return path[:len(scope)] == scope
    return path == scope
