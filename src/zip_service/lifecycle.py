"""Concurrency limiting and graceful-shutdown bookkeeping for zip requests."""

import asyncio
import time
from typing import Protocol


class Abortable(Protocol):
    def abort_for_shutdown(self) -> None: ...


class InflightTracker:
    """Counts in-flight zip requests and rejects (never queues) requests over the limit.

    Only touched from the event loop thread, so no locking is needed.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0
        self._requests: set[Abortable] = set()
        self.shutting_down = False

    @property
    def inflight(self) -> int:
        return self._count

    def try_acquire(self) -> bool:
        if self._count >= self._limit:
            return False
        self._count += 1
        return True

    def track(self, request: Abortable) -> None:
        self._requests.add(request)

    def release(self, request: Abortable | None = None) -> None:
        self._count -= 1
        if request is not None:
            self._requests.discard(request)

    def begin_shutdown(self) -> None:
        self.shutting_down = True

    def abort_all(self) -> None:
        for request in list(self._requests):
            request.abort_for_shutdown()

    async def wait_idle(self, timeout: float) -> bool:
        """Wait until no requests are in flight; False if `timeout` passes first."""
        deadline = time.monotonic() + timeout
        while self._count > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True
