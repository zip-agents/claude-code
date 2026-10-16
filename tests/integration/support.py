"""Helpers for integration tests: a real server subprocess and a raw HTTP/1.1 client.

The raw client exists because the behaviours under test (duplex streaming, stalled
readers, truncated chunked responses) are invisible to high-level HTTP clients.
"""

import hashlib
import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.multipart_builder import CONTENT_TYPE

SETTING_NAMES = {
    "PORT",
    "LOG_LEVEL",
    "ZIP_COMPRESSION_LEVEL",
    "SHUTDOWN_GRACE_SECONDS",
    "MAX_FILES",
    "MAX_CONCURRENT_REQUESTS",
    "IDLE_TIMEOUT_SECONDS",
    "MAX_REQUEST_DURATION_SECONDS",
    "OUTPUT_BUFFER_MAX_BYTES",
}
MIB = 1024 * 1024
MAIN_SCRIPT = Path(__file__).resolve().parents[2] / "main.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class ServerProcess:
    process: subprocess.Popen[bytes]
    port: int
    workdir: Path
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int:
        return self.process.pid

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in list(self.stdout_lines)]

    def completion_logs(self) -> list[dict[str, Any]]:
        return [record for record in self.logs() if "outcome" in record]

    def wait_for_log(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for record in self.logs():
                if predicate(record):
                    return record
            time.sleep(0.05)
        raise AssertionError(f"no matching log line; logs were:\n{self.stdout_lines}")

    def completion_for(self, request_id: str, timeout: float = 10) -> dict[str, Any]:
        return self.wait_for_log(
            lambda r: r.get("request_id") == request_id and "outcome" in r, timeout
        )

    def signal(self, signum: int = signal.SIGTERM) -> None:
        self.process.send_signal(signum)

    def stop(self, timeout: float = 30) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        return self.process.returncode


def _pump(stream: Any, sink: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        sink.append(raw.decode("utf-8", errors="replace").rstrip("\n"))


def start_server(workdir: Path, **settings: object) -> ServerProcess:
    """Start `python main.py` with `workdir` as cwd and TMPDIR, and wait until live."""
    port = free_port()
    workdir.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in SETTING_NAMES}
    env.update({"PORT": str(port), "TMPDIR": str(workdir), "PYTHONUNBUFFERED": "1"})
    env.update({key.upper(): str(value) for key, value in settings.items()})
    process = subprocess.Popen(
        [sys.executable, str(MAIN_SCRIPT)],
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    server = ServerProcess(process, port, workdir)
    pumps = ((process.stdout, server.stdout_lines), (process.stderr, server.stderr_lines))
    for stream, sink in pumps:
        threading.Thread(target=_pump, args=(stream, sink), daemon=True).start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            time.sleep(0.2)
            raise RuntimeError(f"server exited early: {server.stderr_lines} {server.stdout_lines}")
        try:
            with urllib.request.urlopen(f"{server.url}/health/live", timeout=1) as response:
                if response.status == 200:
                    return server
        except OSError:
            time.sleep(0.1)
    server.stop()
    raise RuntimeError("server did not become live in time")


# --- raw HTTP/1.1 ------------------------------------------------------------------------


@dataclass
class ChunkedResult:
    complete: bool  # True only if the terminating zero-length chunk arrived
    size: int


class RawClient:
    """A single HTTP/1.1 connection with chunked request bodies and manual reading."""

    def __init__(self, port: int, timeout: float = 30) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self._buffer = b""

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "RawClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send_head(
        self,
        path: str = "/api/v1/zip",
        content_type: str = CONTENT_TYPE,
        headers: dict[str, str] | None = None,
    ) -> None:
        lines = [
            f"POST {path} HTTP/1.1",
            "Host: 127.0.0.1",
            f"Content-Type: {content_type}",
            "Transfer-Encoding: chunked",
        ]
        lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def send_chunk(self, data: bytes) -> None:
        if data:
            self.sock.sendall(b"%x\r\n" % len(data) + data + b"\r\n")

    def send_end(self) -> None:
        self.sock.sendall(b"0\r\n\r\n")

    def send_body(self, chunks: Iterable[bytes]) -> None:
        for chunk in chunks:
            self.send_chunk(chunk)
        self.send_end()

    def _fill(self) -> bool:
        try:
            data = self.sock.recv(256 * 1024)
        except (ConnectionResetError, BrokenPipeError):
            return False
        if not data:
            return False
        self._buffer += data
        return True

    def _read_line(self) -> bytes | None:
        while b"\r\n" not in self._buffer:
            if not self._fill():
                return None
        line, self._buffer = self._buffer.split(b"\r\n", 1)
        return line

    def _read_exact(self, size: int) -> bytes | None:
        while len(self._buffer) < size:
            if not self._fill():
                return None
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def read_head(self) -> tuple[int, dict[str, str]]:
        """Read the status line and headers, skipping any interim 1xx responses."""
        while True:
            status_line = self._read_line()
            if status_line is None:
                raise ConnectionError("connection closed before a response")
            status = int(status_line.split()[1])
            headers: dict[str, str] = {}
            while (line := self._read_line()) not in (b"", None):
                assert line is not None
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            if status >= 200:
                return status, headers

    def read_chunked(self, sink: Callable[[bytes], object] | None = None) -> ChunkedResult:
        total = 0
        while True:
            size_line = self._read_line()
            if size_line is None:
                return ChunkedResult(False, total)
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self._read_line()
                return ChunkedResult(True, total)
            data = self._read_exact(size)
            if data is None or self._read_exact(2) is None:
                return ChunkedResult(False, total)
            total += size
            if sink:
                sink(data)

    def read_sized(self, length: int) -> bytes:
        data = self._read_exact(length)
        assert data is not None
        return data

    def read_problem(self) -> tuple[int, dict[str, str], dict[str, Any]]:
        status, headers = self.read_head()
        problem = json.loads(self.read_sized(int(headers["content-length"])))
        return status, headers, problem


# --- deterministic payloads --------------------------------------------------------------


def random_chunks(total: int, seed: int, chunk_size: int = MIB) -> Iterator[bytes]:
    """Incompressible pseudo-random bytes; one random block repeated, which DEFLATE's
    32 KiB window cannot exploit."""
    block = random.Random(seed).randbytes(chunk_size)
    remaining = total
    while remaining > 0:
        piece = block[: min(chunk_size, remaining)]
        remaining -= len(piece)
        yield piece


def sha256_of(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def rss_bytes(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS not found")


class RssSampler(threading.Thread):
    def __init__(self, pid: int, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.peak = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.peak = max(self.peak, rss_bytes(self.pid))
            except (OSError, RuntimeError):
                return
            time.sleep(self.interval)

    def stop(self) -> int:
        self._stop_event.set()
        self.join()
        return self.peak
