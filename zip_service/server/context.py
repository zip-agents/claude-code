"""Per-request context shared between the middleware, routes and the zip pipeline."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["completed", "rejected", "aborted", "client_disconnected"]

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# ASGI scope extension set by the server's protocol: a callable `close(grace_seconds)` that
# closes the connection, forcibly if buffered output cannot be flushed within the grace.
CLOSE_CONNECTION_EXTENSION = "zip_service.close_connection"


@dataclass
class RequestStats:
    """Mutable facts about one zip request, emitted in its completion log line."""

    status: int | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    files_count: int = 0
    outcome: Outcome | None = None
    code: str | None = None
    client_caused: bool = True


stats_var: ContextVar[RequestStats | None] = ContextVar("request_stats", default=None)


def current_stats() -> RequestStats:
    """The stats for the current request; a detached instance outside a tracked request."""
    stats = stats_var.get()
    return stats if stats is not None else RequestStats()


def record_rejection(code: str) -> None:
    """Mark the current request as rejected with an error response (before streaming)."""
    stats = stats_var.get()
    if stats is not None and stats.outcome is None:
        stats.outcome = "rejected"
        stats.code = code
