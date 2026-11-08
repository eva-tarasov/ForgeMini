import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = record.http_fields
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": fields["request_id"],
            "method": fields["method"],
            "route": fields["route"],
            "status_code": fields["status_code"],
            "duration_ms": fields["duration_ms"],
        }
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def configure_http_logger() -> logging.Logger:
    logger = logging.getLogger("forge.http")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(handler.name == "forge.http.json" for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name("forge.http.json")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
