"""JSON-lines logging to stdout. Nothing else may write to stdout."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from zip_service.server.context import request_id_var

# uvicorn logs this when an ASGI app returns after starting but not finishing a response.
# That is exactly how a mid-stream abort closes the connection, and the pipeline already
# logs the failure with its code, so the message is noise.
_INCOMPLETE_RESPONSE_MESSAGE = "ASGI callable returned without completing response"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _DropAbortNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "uvicorn.error"
            and record.getMessage().startswith(_INCOMPLETE_RESPONSE_MESSAGE)
        )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_DropAbortNoise())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's loggers propagate to root when uvicorn is run with log_config=None.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
