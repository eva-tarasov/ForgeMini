"""Canonical digest of a JSON-compatible value (ADR-003, cut 3: it lived in the M1 evidence module)."""
import json
from hashlib import sha256


def metadata_digest(value: object) -> str:
    """SHA-256 of the canonical JSON: sorted keys, ASCII, no spaces, NaN rejected. Equal data => equal digest."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()
