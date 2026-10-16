"""Per-request streaming orchestration: request body in, zip archive out.

Three tasks run per request:

* reader   - receives body chunks, runs the processor in a worker thread, sends the
             response headers when streaming begins and puts output in the buffer;
* writer   - drains the buffer to the client;
* watchdog - enforces the idle and total-duration timeouts.

The OutputBuffer is the only place output accumulates. When it is full the reader blocks,
stops calling receive() and the server stops reading the socket, so memory stays bounded
while a client that is not reading the response catches up (contract §4.1).
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from zip_service.config import Settings
from zip_service.context import CLOSE_CONNECTION_EXTENSION, RequestStats
from zip_service.lifecycle import InflightTracker
from zip_service.names import content_disposition
from zip_service.problems import ProblemError, problem_response
from zip_service.processor import ArchiveProcessor

logger = logging.getLogger(__name__)

# Status recorded for a client that disconnects before any response was sent (nginx's
# convention); nothing is actually sent.
CLIENT_CLOSED_REQUEST = 499
# After a mid-stream abort, output already handed to the server gets this long to flush
# before the connection is forcibly reset (a client that is not reading never drains it).
ABORT_FLUSH_GRACE_SECONDS = 1.0
_MAX_WATCHDOG_TICK = 0.25


class OutputBuffer:
    """A byte-bounded FIFO. `put` waits while the buffer holds `max_bytes` or more.

    A single chunk may take the buffer past `max_bytes`, so the true bound is
    `max_bytes` plus one chunk (at most one processed input chunk's worth of output).
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._closed = False
        self._changed = asyncio.Condition()

    @property
    def size(self) -> int:
        return self._size

    async def put(self, chunk: bytes) -> None:
        if not chunk:
            return
        async with self._changed:
            await self._changed.wait_for(lambda: self._size < self._max_bytes)
            self._chunks.append(chunk)
            self._size += len(chunk)
            self._changed.notify_all()

    async def close(self) -> None:
        async with self._changed:
            self._closed = True
            self._changed.notify_all()

    async def get(self) -> bytes | None:
        """Next chunk, or None once the buffer is closed and empty."""
        async with self._changed:
            await self._changed.wait_for(lambda: bool(self._chunks) or self._closed)
            if not self._chunks:
                return None
            chunk = self._chunks.popleft()
            self._size -= len(chunk)
            self._changed.notify_all()
            return chunk


@dataclass(frozen=True)
class _Failure:
    code: str
    client_caused: bool
    disconnected: bool = False
    detail: str | None = None


class _ClientDisconnected(Exception):
    pass


class ZipPipeline(Response):
    """The response object for POST /api/v1/zip; owns receive/send for the request."""

    def __init__(
        self,
        *,
        boundary: bytes,
        settings: Settings,
        tracker: InflightTracker,
        stats: RequestStats,
        received_at: datetime,
    ) -> None:
        super().__init__(status_code=200)
        self._settings = settings
        self._tracker = tracker
        self._stats = stats
        self._processor = ArchiveProcessor(
            boundary,
            max_files=settings.max_files,
            compression_level=settings.zip_compression_level,
            modified=received_at,
        )
        self._buffer = OutputBuffer(settings.output_buffer_max_bytes)
        self._done: asyncio.Future[_Failure | None] = asyncio.get_running_loop().create_future()
        self._headers_sent = False
        self._response_complete = False
        self._started = time.monotonic()
        self._last_progress = self._started

    def abort_for_shutdown(self) -> None:
        self._finish(_Failure("shutting-down", client_caused=False))

    def _finish(self, failure: _Failure | None) -> None:
        if not self._done.done():
            self._done.set_result(failure)

    def _progress(self) -> None:
        self._last_progress = time.monotonic()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            tasks = [
                asyncio.create_task(self._guard(self._read(receive, send))),
                asyncio.create_task(self._guard(self._write(send))),
                asyncio.create_task(self._guard(self._watchdog())),
            ]
            try:
                failure = await self._done
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self._stats.files_count = self._processor.files_count
            if failure is None:
                self._stats.outcome = "completed"
                self._stats.status = 200
            else:
                await self._fail(failure, scope, receive, send)
        finally:
            self._tracker.release(self)

    async def _guard(self, work: Coroutine[Any, Any, None]) -> None:
        """Turn any task failure into the request's single outcome."""
        try:
            await work
        except ProblemError as exc:
            self._finish(_Failure(exc.code, client_caused=exc.status < 500, detail=exc.detail))
        except _ClientDisconnected:
            self._finish(_Failure("client-disconnected", client_caused=True, disconnected=True))
        except Exception:
            logger.exception("Unexpected error while producing the archive")
            self._finish(_Failure("internal-error", client_caused=False))

    async def _read(self, receive: Receive, send: Send) -> None:
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientDisconnected
            chunk: bytes = message.get("body", b"")
            more_body = message.get("more_body", False)
            if chunk:
                self._stats.bytes_in += len(chunk)
                self._progress()
                output = await asyncio.to_thread(self._processor.feed, chunk)
                await self._emit(output, send)
        await self._emit(await asyncio.to_thread(self._processor.finish), send)
        await self._buffer.close()
        # Keep listening so a disconnect while the remaining output drains is noticed.
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                if self._response_complete:
                    return
                raise _ClientDisconnected

    async def _emit(self, output: bytes, send: Send) -> None:
        if self._processor.streaming and not self._headers_sent:
            await self._send_headers(send)
        await self._buffer.put(output)

    async def _send_headers(self, send: Send) -> None:
        name = self._processor.archive_name
        assert name is not None, "streaming has begun, so the archive name is known"
        headers = [
            (b"content-type", b"application/zip"),
            (b"content-disposition", content_disposition(name).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]
        self._headers_sent = True
        await self._send(send, {"type": "http.response.start", "status": 200, "headers": headers})
        self._progress()

    async def _write(self, send: Send) -> None:
        while (chunk := await self._buffer.get()) is not None:
            await self._send(send, {"type": "http.response.body", "body": chunk, "more_body": True})
            self._progress()
        self._response_complete = True
        await self._send(send, {"type": "http.response.body", "body": b"", "more_body": False})
        self._finish(None)

    @staticmethod
    async def _send(send: Send, message: Message) -> None:
        try:
            await send(message)
        except OSError as exc:  # uvicorn raises ClientDisconnected (an OSError) when gone
            raise _ClientDisconnected from exc

    async def _watchdog(self) -> None:
        idle = self._settings.idle_timeout_seconds
        deadline = self._started + self._settings.max_request_duration_seconds
        tick = min(_MAX_WATCHDOG_TICK, idle / 4)
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            if now >= deadline:
                raise ProblemError("request-timeout", "The request exceeded its maximum duration.")
            if now - self._last_progress >= idle:
                raise ProblemError("request-timeout", "The request made no progress in time.")

    async def _fail(self, failure: _Failure, scope: Scope, receive: Receive, send: Send) -> None:
        stats = self._stats
        stats.code = failure.code
        stats.client_caused = failure.client_caused
        if failure.disconnected:
            stats.outcome = "client_disconnected"
            stats.status = 200 if self._headers_sent else CLIENT_CLOSED_REQUEST
            return
        if not self._headers_sent and not self._processor.streaming:
            stats.outcome = "rejected"
            # A stalled client will not send the rest of its body; don't wait for it.
            headers = {"Connection": "close"} if failure.code == "request-timeout" else None
            response = problem_response(failure.code, failure.detail, headers)
            stats.status = response.status_code
            try:
                await response(scope, receive, send)
            except OSError:
                stats.outcome = "client_disconnected"
            return
        # Mid-stream (§6.2): the status line is (or must be) 200. Returning without the
        # final body message makes the server close the connection without the
        # terminating chunk, so the client sees a truncated archive.
        stats.outcome = "aborted"
        stats.status = 200
        if not self._headers_sent:
            try:
                await self._send_headers(send)
            except _ClientDisconnected:
                stats.outcome = "client_disconnected"
        close_connection = scope.get("extensions", {}).get(CLOSE_CONNECTION_EXTENSION)
        if close_connection is not None:
            close_connection(ABORT_FLUSH_GRACE_SECONDS)
