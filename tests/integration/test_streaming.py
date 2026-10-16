"""Streaming, duplex, abort and concurrency behaviour against a real server (§4.1, §6.2)."""

import contextlib
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import ServerFactory
from tests.integration.support import (
    MIB,
    ChunkedResult,
    RawClient,
    ServerProcess,
    random_chunks,
    sha256_of,
)
from tests.multipart_builder import BOUNDARY, Part, body, field_part, file_part, iter_body

pytestmark = pytest.mark.slow


def _read_zip_response(client: RawClient, path: Path) -> tuple[int, dict[str, str], ChunkedResult]:
    status, headers = client.read_head()
    with path.open("wb") as sink:
        result = client.read_chunked(sink.write)
    return status, headers, result


def _entry_sha256(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as archive, archive.open(name) as entry:
        return sha256_of(iter(lambda: entry.read(MIB), b""))


def test_half_duplex_client_succeeds_when_output_fits_in_buffer(
    server: ServerProcess, tmp_path: Path
) -> None:
    # Send the entire request before reading a single byte of the response.
    data = b"compressible text line\n" * 40_000  # ~0.9 MiB in, far less out
    with RawClient(server.port) as client:
        client.send_head()
        client.send_body(iter_body([file_part("text.txt", data)]))
        status, _, result = _read_zip_response(client, tmp_path / "half.zip")
    assert status == 200
    assert result.complete
    with zipfile.ZipFile(tmp_path / "half.zip") as archive:
        assert archive.testzip() is None
        assert archive.read("text.txt") == data


def test_half_duplex_incompressible_output_within_buffer(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    server = make_server(OUTPUT_BUFFER_MAX_BYTES=8 * MIB)
    size = 4 * MIB  # output is ~4 MiB: fits in the 8 MiB buffer (plus socket buffers)
    with RawClient(server.port) as client:
        client.send_head()
        client.send_body(iter_body([file_part("r.bin", random_chunks(size, seed=2))]))
        status, _, result = _read_zip_response(client, tmp_path / "half.zip")
    assert status == 200
    assert result.complete
    assert _entry_sha256(tmp_path / "half.zip", "r.bin") == sha256_of(random_chunks(size, seed=2))


def test_full_duplex_client_succeeds_far_beyond_buffer(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    buffer = 512 * 1024
    size = 64 * MIB  # 128x the output buffer
    server = make_server(OUTPUT_BUFFER_MAX_BYTES=buffer, ZIP_COMPRESSION_LEVEL=1)
    path = tmp_path / "full.zip"
    with RawClient(server.port, timeout=120) as client:
        client.send_head(headers={"X-Request-ID": "full-duplex"})
        sender = threading.Thread(
            target=client.send_body,
            args=(iter_body([file_part("big.bin", random_chunks(size, seed=3))]),),
        )
        sender.start()
        status, _, result = _read_zip_response(client, path)
        sender.join()
    assert status == 200
    assert result.complete
    assert _entry_sha256(path, "big.bin") == sha256_of(random_chunks(size, seed=3))
    record = server.completion_for("full-duplex")
    assert record["outcome"] == "completed"
    assert record["bytes_in"] > size
    assert record["bytes_out"] == path.stat().st_size
    assert record["files_count"] == 1


def test_archive_bytes_arrive_before_upload_finishes(
    server: ServerProcess, tmp_path: Path
) -> None:
    """End-to-end streaming: compressed bytes of a file reach the client while that same
    file is still being uploaded, not after the upload completes."""
    first_half = 8 * MIB
    total = 16 * MIB
    expect_early = 4 * MIB  # far more than headers; only file content can account for it
    # Incompressible data, so DEFLATE cannot legitimately hold output back.
    chunks = list(random_chunks(total, seed=8))
    part = file_part("big.bin", chunks)
    release_rest = threading.Event()
    path = tmp_path / "early.zip"

    with RawClient(server.port) as client:
        def upload() -> None:
            client.send_head(headers={"X-Request-ID": "early-bytes"})
            client.send_chunk(part.head())
            for chunk in chunks[: first_half // MIB]:
                client.send_chunk(chunk)
            # The upload stays unfinished (no closing boundary) until the test says so.
            release_rest.wait(60)
            for chunk in chunks[first_half // MIB :]:
                client.send_chunk(chunk)
            client.send_chunk(f"\r\n--{BOUNDARY}--\r\n".encode())
            client.send_end()

        sender = threading.Thread(target=upload)
        sender.start()
        client.sock.settimeout(15)
        try:
            status, headers = client.read_head()
            with path.open("wb") as sink:
                early = client.read_chunked(sink.write, stop_after=expect_early)
                assert not release_rest.is_set(), "the upload must still be in progress"
                assert early.size >= expect_early, (
                    f"only {early.size} archive bytes arrived while the upload was in progress"
                )
                release_rest.set()
                rest = client.read_chunked(sink.write)
        except TimeoutError:
            pytest.fail("no archive bytes arrived while the upload was still in progress")
        finally:
            release_rest.set()
            sender.join()

    assert status == 200
    assert headers["content-type"] == "application/zip"
    assert path.read_bytes()[:4] == b"PK\x03\x04"
    assert rest.complete
    assert _entry_sha256(path, "big.bin") == sha256_of(chunks)
    assert server.completion_for("early-bytes")["outcome"] == "completed"


def test_stalled_client_is_aborted_after_idle_timeout(make_server: ServerFactory) -> None:
    server = make_server(IDLE_TIMEOUT_SECONDS=2, OUTPUT_BUFFER_MAX_BYTES=256 * 1024)
    client = RawClient(server.port, timeout=30)
    client.send_head(headers={"X-Request-ID": "stalled"})
    # Upload far more incompressible data than the buffer and socket buffers can absorb,
    # without ever reading: the server must stop reading, then abort and reset.
    error: OSError | None = None
    try:
        for chunk in iter_body([file_part("r.bin", random_chunks(512 * MIB, seed=4))]):
            client.send_chunk(chunk)
    except OSError as exc:
        error = exc
    assert error is not None, "the whole upload was accepted without backpressure"
    assert not isinstance(error, TimeoutError), "the server never closed the connection"
    record = server.completion_for("stalled", timeout=15)
    assert record["outcome"] == "aborted"
    assert record["code"] == "request-timeout"
    assert record["status"] == 200
    assert record["level"] == "WARNING"
    assert record["bytes_in"] < 128 * MIB
    client.close()


def _assert_truncated(client: RawClient, tmp_path: Path) -> None:
    status, headers, result = _read_zip_response(client, tmp_path / "truncated.zip")
    assert status == 200
    assert headers["content-type"] == "application/zip"
    assert not result.complete, "the terminating zero-length chunk must not be sent"
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(tmp_path / "truncated.zip").testzip()


@pytest.mark.parametrize(
    ("parts_tail", "code"),
    [
        ([file_part("b.txt", b"two", name="other")], "unexpected-file-field"),
        ([file_part("b.txt", b"two"), file_part("c.txt", b"three")], "too-many-files"),
    ],
)
def test_mid_stream_failure_truncates_and_closes(
    make_server: ServerFactory, tmp_path: Path, parts_tail: list[Part], code: str
) -> None:
    server = make_server(MAX_FILES=2)
    request_id = f"mid-{code}"
    with RawClient(server.port) as client:
        client.send_head(headers={"X-Request-ID": request_id})
        client.send_body(iter_body([file_part("a.txt", b"one" * 1000), *parts_tail]))
        _assert_truncated(client, tmp_path)
    record = server.completion_for(request_id)
    assert (record["outcome"], record["code"], record["status"]) == ("aborted", code, 200)
    assert record["level"] == "WARNING"


def test_mid_stream_malformed_multipart(server: ServerProcess, tmp_path: Path) -> None:
    with RawClient(server.port) as client:
        client.send_head(headers={"X-Request-ID": "mid-malformed"})
        client.send_chunk(body([file_part("a.txt", b"one")], close=False))
        client.send_chunk(f"--{BOUNDARY}\r\nno-colon-header-line\r\n\r\n".encode())
        client.send_end()
        _assert_truncated(client, tmp_path)
    record = server.completion_for("mid-malformed")
    assert (record["outcome"], record["code"]) == ("aborted", "malformed-request")


def test_body_ending_without_closing_boundary_mid_stream(
    server: ServerProcess, tmp_path: Path
) -> None:
    with RawClient(server.port) as client:
        client.send_head()
        client.send_body([body([file_part("a.txt", b"x")], close=False)])
        _assert_truncated(client, tmp_path)


def test_idle_timeout_after_streaming_started(make_server: ServerFactory, tmp_path: Path) -> None:
    server = make_server(IDLE_TIMEOUT_SECONDS=1)
    with RawClient(server.port) as client:
        client.send_head(headers={"X-Request-ID": "idle-mid"})
        client.send_chunk(b"".join(iter_body([file_part("a.txt", b"partial")], close=False))[:-2])
        _assert_truncated(client, tmp_path)
    record = server.completion_for("idle-mid")
    assert (record["outcome"], record["code"]) == ("aborted", "request-timeout")


def _hold_request_open(server: ServerProcess, request_id: str) -> RawClient:
    """Start a zip request (streaming begun) and keep it in flight."""
    client = RawClient(server.port)
    client.send_head(headers={"X-Request-ID": request_id})
    client.send_chunk(file_part("slow.txt").head())
    assert client.read_head()[0] == 200
    return client


def test_concurrency_limit_returns_503_overloaded(make_server: ServerFactory) -> None:
    server = make_server(MAX_CONCURRENT_REQUESTS=1, IDLE_TIMEOUT_SECONDS=10)
    holder = _hold_request_open(server, "holder")
    try:
        response = httpx.post(
            f"{server.url}/api/v1/zip",
            content=body([file_part("a", b"a")]),
            headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert response.json()["code"] == "overloaded"
        # Health checks are not subject to the limit.
        assert httpx.get(f"{server.url}/health/live").status_code == 200
        assert httpx.get(f"{server.url}/health/ready").status_code == 200
    finally:
        holder.close()


def test_client_disconnect_releases_resources(make_server: ServerFactory) -> None:
    server = make_server(MAX_CONCURRENT_REQUESTS=1, IDLE_TIMEOUT_SECONDS=30)
    holder = _hold_request_open(server, "disconnector")
    holder.send_chunk(b"some data")
    holder.close()
    record = server.completion_for("disconnector", timeout=5)
    assert record["outcome"] == "client_disconnected"
    # The slot is free again well before the 30 s idle timeout.
    response = httpx.post(
        f"{server.url}/api/v1/zip",
        content=body([file_part("a", b"a")]),
        headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    )
    assert response.status_code == 200


def test_disconnect_while_server_is_blocked_on_output(make_server: ServerFactory) -> None:
    server = make_server(
        MAX_CONCURRENT_REQUESTS=1, IDLE_TIMEOUT_SECONDS=30, OUTPUT_BUFFER_MAX_BYTES=256 * 1024
    )
    client = RawClient(server.port)
    client.send_head(headers={"X-Request-ID": "blocked-disconnect"})
    chunks = iter_body([file_part("r.bin", random_chunks(256 * MIB, seed=5))])
    client.sock.settimeout(2)
    with contextlib.suppress(TimeoutError):
        for chunk in chunks:  # until the server stops reading and our send blocks
            client.send_chunk(chunk)
    client.close()
    record = server.completion_for("blocked-disconnect", timeout=10)
    assert record["outcome"] == "client_disconnected"


def test_health_stays_responsive_during_heavy_compression(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    server = make_server(ZIP_COMPRESSION_LEVEL=9)
    size = 96 * MIB

    def upload() -> None:
        with RawClient(server.port, timeout=120) as client:
            client.send_head()
            sender = threading.Thread(
                target=client.send_body,
                args=(iter_body([file_part("big.bin", random_chunks(size, seed=6))]),),
            )
            sender.start()
            _read_zip_response(client, tmp_path / "heavy.zip")
            sender.join()

    uploader = threading.Thread(target=upload)
    uploader.start()
    time.sleep(0.5)
    latencies: list[float] = []
    while uploader.is_alive() and len(latencies) < 40:
        started = time.monotonic()
        assert httpx.get(f"{server.url}/health/live", timeout=5).status_code == 200
        latencies.append(time.monotonic() - started)
        time.sleep(0.05)
    uploader.join()
    assert latencies, "upload finished before any health check ran"
    assert max(latencies) < 0.5, latencies


def test_fields_after_first_file_are_ignored_and_logged(
    server: ServerProcess, tmp_path: Path
) -> None:
    payload = body([file_part("a.txt", b"a"), field_part("filename", "late")])
    with RawClient(server.port) as client:
        client.send_head(headers={"X-Request-ID": "late-field"})
        client.send_body([payload])
        status, headers, result = _read_zip_response(client, tmp_path / "late.zip")
    assert status == 200
    assert result.complete
    assert 'filename="zipped_files.zip"' in headers["content-disposition"]
    server.wait_for_log(lambda r: r.get("request_id") == "late-field" and r["level"] == "WARNING")
