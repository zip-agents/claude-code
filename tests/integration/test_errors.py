"""Every §6.1 error reachable before streaming begins, against a real server."""

import time
import uuid

import httpx
import pytest

from tests.integration.conftest import ServerFactory
from tests.integration.support import RawClient, ServerProcess
from tests.multipart_builder import BOUNDARY, CONTENT_TYPE, body, field_part, file_part

pytestmark = pytest.mark.slow


def _assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem == {
        "type": f"urn:zip-service:problem:{code}",
        "title": problem["title"],
        "status": status,
        "detail": problem["detail"],
        "code": code,
        "request_id": response.headers["x-request-id"],
    }
    assert problem["title"]
    assert problem["detail"]


def _post(
    server: ServerProcess, payload: bytes, content_type: str = CONTENT_TYPE
) -> httpx.Response:
    return httpx.post(
        f"{server.url}/api/v1/zip",
        content=payload,
        headers={"content-type": content_type},
        timeout=30,
    )


@pytest.mark.parametrize(
    ("content_type", "payload", "status", "code"),
    [
        ("application/json", b"{}", 415, "unsupported-media-type"),
        ("text/plain", b"hello", 415, "unsupported-media-type"),
        ("multipart/form-data", b"x", 400, "malformed-request"),
        (CONTENT_TYPE, b"definitely not multipart", 400, "malformed-request"),
        (CONTENT_TYPE, body([field_part("note", "x")], close=False), 400, "malformed-request"),
        (CONTENT_TYPE, body([field_part("note", "hi")]), 400, "no-files"),
        (CONTENT_TYPE, body([file_part("", b"")]), 400, "no-files"),
        (
            CONTENT_TYPE,
            body([file_part("a.txt", b"x", name="upload")]),
            400,
            "unexpected-file-field",
        ),
        (
            CONTENT_TYPE,
            body([field_part("filename", "a"), field_part("filename", "b"), file_part("f", b"")]),
            422,
            "invalid-archive-name",
        ),
        (
            CONTENT_TYPE,
            body([field_part("filename", "x" * 1025), file_part("f", b"")]),
            422,
            "invalid-archive-name",
        ),
        (
            CONTENT_TYPE,
            body([field_part("filename", "bad/name"), file_part("f", b"")]),
            422,
            "invalid-archive-name",
        ),
        (
            CONTENT_TYPE,
            body([field_part("filename", "ctrl\x01"), file_part("f", b"")]),
            422,
            "invalid-archive-name",
        ),
        (
            CONTENT_TYPE,
            body([field_part("filename", "n" * 252), file_part("f", b"")]),
            422,
            "invalid-archive-name",
        ),
    ],
)
def test_error_responses(
    server: ServerProcess, content_type: str, payload: bytes, status: int, code: str
) -> None:
    response = _post(server, payload, content_type)
    _assert_problem(response, status, code)
    record = server.completion_for(response.headers["x-request-id"])
    assert record["outcome"] == "rejected"
    assert record["code"] == code
    assert record["status"] == status


def test_not_found_and_method_not_allowed(server: ServerProcess) -> None:
    _assert_problem(httpx.get(f"{server.url}/nope"), 404, "not-found")
    _assert_problem(httpx.get(f"{server.url}/api/v1/zip"), 405, "method-not-allowed")
    _assert_problem(httpx.put(f"{server.url}/health/ready"), 405, "method-not-allowed")


def test_request_id_handling(server: ServerProcess) -> None:
    response = httpx.get(f"{server.url}/nope", headers={"x-request-id": "trace-42.a_b"})
    assert response.headers["x-request-id"] == "trace-42.a_b"
    assert response.json()["request_id"] == "trace-42.a_b"
    generated = httpx.get(f"{server.url}/health/live", headers={"x-request-id": "bad id!"})
    assert uuid.UUID(generated.headers["x-request-id"]).version == 4


def test_request_timeout_before_streaming(make_server: ServerFactory) -> None:
    server = make_server(IDLE_TIMEOUT_SECONDS=1)
    with RawClient(server.port) as client:
        client.send_head(headers={"X-Request-ID": "stall-before-stream"})
        client.send_chunk(f"--{BOUNDARY}\r\nContent-Disposition: form-da".encode())
        started = time.monotonic()
        status, headers, problem = client.read_problem()
        elapsed = time.monotonic() - started
    assert status == 408
    assert headers["content-type"] == "application/problem+json"
    assert problem["code"] == "request-timeout"
    assert problem["request_id"] == "stall-before-stream"
    assert 0.8 <= elapsed < 5
    record = server.completion_for("stall-before-stream")
    assert (record["outcome"], record["code"], record["status"]) == (
        "rejected",
        "request-timeout",
        408,
    )


def test_total_duration_timeout_before_streaming(make_server: ServerFactory) -> None:
    server = make_server(IDLE_TIMEOUT_SECONDS=10, MAX_REQUEST_DURATION_SECONDS=2)
    with RawClient(server.port) as client:
        client.send_head()
        started = time.monotonic()
        # Make steady progress for 1.5 s, then go quiet but stay well inside the 10 s idle
        # timeout: only the 2 s total duration can expire. Stop sending before the deadline,
        # since writing to the closed connection would reset it and lose the response.
        preamble = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
        client.send_chunk(preamble.encode())
        while time.monotonic() - started < 1.5:
            client.send_chunk(b"x")
            time.sleep(0.1)
        status, _, problem = client.read_problem()
        elapsed = time.monotonic() - started
    assert status == 408
    assert problem["code"] == "request-timeout"
    assert 1.5 <= elapsed < 5


def test_too_many_files_is_never_reachable_before_streaming(make_server: ServerFactory) -> None:
    # Documented choice: headers are sent when the first file part starts and MAX_FILES >= 1,
    # so exceeding MAX_FILES always happens mid-stream (see test_streaming).
    server = make_server(MAX_FILES=1)
    response = _post(server, body([file_part("only.txt", b"1")]))
    assert response.status_code == 200
