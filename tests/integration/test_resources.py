"""Bounded memory and no disk writes during large uploads (contract §1)."""

import os
import sys
import threading
from pathlib import Path

import pytest

from tests.integration.conftest import ServerFactory
from tests.integration.support import (
    MIB,
    RawClient,
    RssSampler,
    ServerProcess,
    random_chunks,
    rss_bytes,
)
from tests.multipart_builder import body, file_part, iter_body

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses /proc"),
]

UPLOAD_BYTES = int(os.environ.get("MEMORY_TEST_UPLOAD_MIB", "256")) * MIB
BUFFER_BYTES = 4 * MIB
# Allocator slack, thread stacks, zlib state and socket buffers; deliberately generous
# but an order of magnitude below the upload size.
OVERHEAD_ALLOWANCE = 40 * MIB


def _upload(server: ServerProcess, size: int, pause_reading_at: int | None = None) -> int:
    """Full-duplex upload; optionally stop reading for 2 s once `pause_reading_at` bytes of
    output arrived, so the server's output buffer fills completely. Returns bytes received."""
    received = 0
    with RawClient(server.port, timeout=120) as client:
        client.send_head()
        sender = threading.Thread(
            target=client.send_body,
            args=(iter_body([file_part("big.bin", random_chunks(size, seed=7))]),),
        )
        sender.start()
        status, _ = client.read_head()
        assert status == 200
        paused = False

        def sink(data: bytes) -> None:
            nonlocal received, paused
            received += len(data)
            if pause_reading_at is not None and not paused and received >= pause_reading_at:
                paused = True
                threading.Event().wait(2)

        result = client.read_chunked(sink)
        sender.join()
        assert result.complete
    return received


def _warm_up(server: ServerProcess) -> None:
    with RawClient(server.port) as client:
        client.send_head()
        client.send_body([body([file_part("warm.txt", b"x" * MIB)])])
        client.read_head()
        assert client.read_chunked().complete


def test_memory_stays_bounded_during_large_upload(make_server: ServerFactory) -> None:
    server = make_server(
        OUTPUT_BUFFER_MAX_BYTES=BUFFER_BYTES, ZIP_COMPRESSION_LEVEL=1, IDLE_TIMEOUT_SECONDS=10
    )
    _warm_up(server)
    baseline = rss_bytes(server.pid)
    sampler = RssSampler(server.pid)
    sampler.start()
    received = _upload(server, UPLOAD_BYTES, pause_reading_at=UPLOAD_BYTES // 4)
    peak = sampler.stop()
    assert received > UPLOAD_BYTES
    growth = peak - baseline
    assert growth < BUFFER_BYTES + OVERHEAD_ALLOWANCE, (
        f"RSS grew by {growth / MIB:.1f} MiB for a {UPLOAD_BYTES / MIB:.0f} MiB upload "
        f"(baseline {baseline / MIB:.1f} MiB, peak {peak / MIB:.1f} MiB)"
    )


def _write_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            if line.startswith("write_bytes:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _files_open_for_writing(pid: int) -> list[str]:
    """Regular files the process holds open with write access."""
    found = []
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
            flags_line = Path(f"/proc/{pid}/fdinfo/{fd}").read_text().splitlines()[1]
        except OSError:
            continue  # closed meanwhile
        flags = int(flags_line.split()[1], 8)
        if target.startswith("/") and not target.startswith("/dev/") and flags & 0o3:
            found.append(target)
    return found


def test_no_upload_data_is_written_to_disk(make_server: ServerFactory, tmp_path: Path) -> None:
    server = make_server(OUTPUT_BUFFER_MAX_BYTES=BUFFER_BYTES, ZIP_COMPRESSION_LEVEL=1)
    before = _write_bytes(server.pid)
    open_for_writing: list[str] = []
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            open_for_writing.extend(_files_open_for_writing(server.pid))
            done.wait(0.05)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        _upload(server, 128 * MIB)
    finally:
        done.set()
        watcher.join()

    assert open_for_writing == []
    # TMPDIR (and the working directory) of the server is an otherwise empty directory.
    assert list(server.workdir.iterdir()) == []
    after = _write_bytes(server.pid)
    if before is not None and after is not None:
        assert after - before < MIB, f"{after - before} bytes written to storage"
