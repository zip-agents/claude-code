"""Entry point: `python main.py` (also the container command)."""

import sys

import uvicorn

from zip_service.api.docs import DocsCatalog, DocsError
from zip_service.app import create_app
from zip_service.config import ConfigError, load_settings
from zip_service.server.lifecycle import InflightTracker
from zip_service.server.logging import configure_logging
from zip_service.server.server import ClosableHttpToolsProtocol, GracefulServer


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        # stdout is reserved for JSON log lines.
        print(exc, file=sys.stderr)
        return 2

    try:
        docs = DocsCatalog.load()
    except DocsError as exc:
        print(f"Invalid image: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    tracker = InflightTracker(settings.max_concurrent_requests)
    config = uvicorn.Config(
        create_app(settings, tracker, docs),
        host="0.0.0.0",
        port=settings.port,
        http=ClosableHttpToolsProtocol,
        loop="asyncio",
        lifespan="off",
        log_config=None,
        access_log=False,
        server_header=False,
        # Backstop only: the drain in GracefulServer has normally finished every request.
        timeout_graceful_shutdown=5,
    )
    GracefulServer(config, tracker, settings.shutdown_grace_seconds).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
