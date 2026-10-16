"""Graceful shutdown on SIGTERM (contract §9)."""

import time
import zipfile
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import ServerFactory
from tests.integration.support import RawClient
from tests.multipart_builder import CONTENT_TYPE, body, file_part

pytestmark = pytest.mark.slow


def _start_inflight(port: int, request_id: str) -> RawClient:
    client = RawClient(port)
    client.send_head(headers={"X-Request-ID": request_id})
    client.send_chunk(file_part("slow.txt").head())
    assert client.read_head()[0] == 200
    return client


def _wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_inflight_request_completes_and_new_work_is_refused(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    server = make_server(SHUTDOWN_GRACE_SECONDS=20, IDLE_TIMEOUT_SECONDS=20)
    client = _start_inflight(server.port, "drained")
    server.signal()

    _wait_until(lambda: httpx.get(f"{server.url}/health/ready").status_code == 503)
    ready = httpx.get(f"{server.url}/health/ready")
    assert ready.json() == {"status": "shutting-down"}
    assert httpx.get(f"{server.url}/health/live").json() == {"status": "ok"}
    rejected = httpx.post(
        f"{server.url}/api/v1/zip",
        content=body([file_part("a", b"a")]),
        headers={"content-type": CONTENT_TYPE},
    )
    assert rejected.status_code == 503
    assert rejected.json()["code"] == "shutting-down"

    # The in-flight request is allowed to finish normally.
    client.send_chunk(b"finished content\r\n")
    client.send_chunk(b"--" + CONTENT_TYPE.split("boundary=")[1].encode() + b"--\r\n")
    client.send_end()
    path = tmp_path / "drained.zip"
    with path.open("wb") as sink:
        result = client.read_chunked(sink.write)
    client.close()
    assert result.complete
    with zipfile.ZipFile(path) as archive:
        assert archive.read("slow.txt") == b"finished content"

    assert server.process.wait(10) == 0
    record = server.completion_for("drained")
    assert record["outcome"] == "completed"


def test_requests_still_running_after_grace_are_aborted(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    server = make_server(SHUTDOWN_GRACE_SECONDS=1, IDLE_TIMEOUT_SECONDS=60)
    client = _start_inflight(server.port, "too-slow")
    started = time.monotonic()
    server.signal()

    result = client.read_chunked()
    client.close()
    assert not result.complete
    assert server.process.wait(15) == 0
    assert time.monotonic() - started < 10
    record = server.completion_for("too-slow")
    assert (record["outcome"], record["code"]) == ("aborted", "shutting-down")
    assert record["level"] == "ERROR"


def test_idle_server_exits_promptly(make_server: ServerFactory) -> None:
    server = make_server(SHUTDOWN_GRACE_SECONDS=25)
    started = time.monotonic()
    server.signal()
    assert server.process.wait(10) == 0
    assert time.monotonic() - started < 5


def test_invalid_configuration_exits_non_zero(make_server: ServerFactory) -> None:
    with pytest.raises(RuntimeError, match="MAX_FILES"):
        make_server(MAX_FILES=0)
