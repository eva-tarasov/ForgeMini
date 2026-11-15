import json
import logging
from datetime import datetime, timezone
from threading import Lock


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


class AgentJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Keep the HTTP logger and agent logger configuration independent of agent services.
        from app.services.agent_observability import agent_log_fields

        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            **agent_log_fields(record.levelname, getattr(record, "agent_fields", None)),
        }
        return json.dumps(entry, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class AgentStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default diagnostic can print the record and the active exception context.
        pass


_agent_configuration_lock = Lock()


def configure_agent_logger() -> logging.Logger:
    logger = logging.getLogger("forge.agent")
    with _agent_configuration_lock:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not any(
            isinstance(handler, AgentStreamHandler) and handler.name == "forge.agent.json"
            for handler in logger.handlers
        ):
            handler = AgentStreamHandler()
            handler.set_name("forge.agent.json")
            handler.setFormatter(AgentJsonFormatter())
            logger.addHandler(handler)
    return logger
