from enum import StrEnum


class Capability(StrEnum):
    REPOSITORY_READ = "repository.read"
    REPOSITORY_WRITE = "repository.write"
