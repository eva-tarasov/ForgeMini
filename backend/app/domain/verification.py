"""The verdict vocabulary of independent verification.

Lives in `domain/` (not next to the M1 verification contract) because the prototype's Verifier, the public API schemas and
the run reads all speak it. Importing it must not pull any M1 module (ADR-003, cut 2).
"""
from enum import StrEnum


class VerificationVerdict(StrEnum):
    PASS = "pass"                    # tests red on pristine source, green with the changeset in a fresh container
    FAIL = "fail"                    # still red / timed out / protected (test) files were touched
    INCONCLUSIVE = "inconclusive"    # evidence cannot prove a fix (already green, or infrastructure broke)
