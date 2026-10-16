import json
import logging
import re

from zip_service.server.context import request_id_var
from zip_service.server.logging import JsonFormatter


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_one_json_object_with_required_fields() -> None:
    line = JsonFormatter().format(_record("hello", fields={"status": 200}))
    payload = json.loads(line)
    assert "\n" not in line
    assert payload["level"] == "WARNING"
    assert payload["message"] == "hello"
    assert payload["status"] == 200
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", payload["timestamp"])
    assert "request_id" not in payload


def test_includes_request_id_from_context() -> None:
    token = request_id_var.set("abc-123")
    try:
        payload = json.loads(JsonFormatter().format(_record("hi")))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "abc-123"
