"""Outermost ASGI middleware: request IDs, last-resort 500s and the completion log line.

Written as plain ASGI rather than BaseHTTPMiddleware, which would buffer and break
streaming. It wraps the whole FastAPI app, including Starlette's ServerErrorMiddleware,
so even framework-generated 500 responses carry X-Request-ID.
"""

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from zip_service.context import RequestStats, request_id_var, stats_var
from zip_service.problems import problem_response

logger = logging.getLogger("zip_service.access")

ZIP_PATH = "/api/v1/zip"
REQUEST_ID_HEADER = b"x-request-id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def resolve_request_id(raw: bytes | None) -> str:
    if raw is not None:
        candidate = raw.decode("latin-1")
        if _VALID_REQUEST_ID.fullmatch(candidate):
            return candidate
    return str(uuid.uuid4())


def _completion_level(stats: RequestStats) -> int:
    if stats.code == "internal-error":
        return logging.ERROR
    if stats.outcome == "aborted":
        return logging.WARNING if stats.client_caused else logging.ERROR
    if stats.outcome == "client_disconnected":
        return logging.WARNING
    return logging.INFO


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER)
        request_id = resolve_request_id(incoming)
        stats = RequestStats()
        id_token = request_id_var.set(request_id)
        stats_token = stats_var.set(stats)
        started = time.monotonic()
        response_started = False

        async def send_with_context(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
                if stats.status is None:
                    stats.status = message["status"]
            elif message["type"] == "http.response.body":
                stats.bytes_out += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception:
            # Starlette's ServerErrorMiddleware has usually sent the problem response
            # already and re-raised; either way this is where the error is logged.
            logger.exception("Unhandled exception")
            stats.outcome = "aborted" if stats.outcome == "completed" else "rejected"
            stats.code = "internal-error"
            if not response_started:
                stats.status = 500
                try:
                    await problem_response("internal-error")(scope, receive, send_with_context)
                except OSError:
                    pass
        finally:
            if scope["method"] == "POST" and scope["path"] == ZIP_PATH:
                self._log_completion(scope, stats, started)
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Request handled",
                    extra={"fields": {"method": scope["method"], "path": scope["path"]}},
                )
            stats_var.reset(stats_token)
            request_id_var.reset(id_token)

    @staticmethod
    def _log_completion(scope: Scope, stats: RequestStats, started: float) -> None:
        outcome = stats.outcome
        if outcome is None:
            outcome = "completed" if (stats.status or 500) < 400 else "rejected"
        fields: dict[str, object] = {
            "method": scope["method"],
            "path": scope["path"],
            "status": stats.status,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "bytes_in": stats.bytes_in,
            "bytes_out": stats.bytes_out,
            "files_count": stats.files_count,
            "outcome": outcome,
        }
        if outcome != "completed":
            fields["code"] = stats.code
        logger.log(_completion_level(stats), f"Zip request {outcome}", extra={"fields": fields})
