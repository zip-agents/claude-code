"""uvicorn server with the graceful shutdown required by contract §9.

uvicorn's own SIGTERM handling stops listening immediately, which would make it
impossible to fail readiness checks or answer new requests with `503 shutting-down`.
Instead, the first signal starts a drain while the server keeps accepting connections;
uvicorn is told to exit only once in-flight requests have finished or been aborted.
"""

import asyncio
import logging
import socket
from types import FrameType

import uvicorn

from zip_service.lifecycle import InflightTracker

logger = logging.getLogger(__name__)

# How long aborted requests get to unwind before uvicorn is told to exit regardless.
_ABORT_SETTLE_SECONDS = 5.0


class GracefulServer(uvicorn.Server):
    def __init__(
        self, config: uvicorn.Config, tracker: InflightTracker, grace_seconds: float
    ) -> None:
        super().__init__(config)
        self._tracker = tracker
        self._grace_seconds = grace_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task[None] | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().startup(sockets=sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Deliberately does not call super(): uvicorn would stop listening at once and,
        # in recent versions, re-raise the signal after shutdown (a non-zero exit).
        if self._loop is None:
            self.should_exit = True
        elif self._tracker.shutting_down:
            logger.warning("Second shutdown signal received; exiting immediately")
            self.force_exit = True
            self.should_exit = True
        else:
            self._tracker.begin_shutdown()
            # Signal handlers may interrupt the loop mid-callback; schedule safely.
            self._loop.call_soon_threadsafe(self._start_drain)

    def _start_drain(self) -> None:
        assert self._loop is not None
        self._drain_task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        logger.info(
            "Shutdown started; draining in-flight requests",
            extra={"fields": {"inflight": self._tracker.inflight}},
        )
        if not await self._tracker.wait_idle(self._grace_seconds):
            logger.warning(
                "Shutdown grace period expired; aborting remaining requests",
                extra={"fields": {"inflight": self._tracker.inflight}},
            )
            self._tracker.abort_all()
            await self._tracker.wait_idle(_ABORT_SETTLE_SECONDS)
        self.should_exit = True
