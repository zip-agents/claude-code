import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.integration.support import MIB, ServerProcess, start_server

# CI sets this so that missing unzip/7z fail the build instead of silently skipping.
REQUIRE_ZIP_TOOLS = os.environ.get("REQUIRE_ZIP_TOOLS") == "1"

ServerFactory = Callable[..., ServerProcess]


def _assert_stdout_is_json_lines(server: ServerProcess) -> None:
    for line in server.stdout_lines:
        record = json.loads(line)  # raises on any non-JSON output
        assert {"timestamp", "level", "message"} <= record.keys(), line


@pytest.fixture
def make_server(tmp_path: Path) -> Iterator[ServerFactory]:
    """Start servers with per-test settings; stopped (and stdout checked) at teardown."""
    servers: list[ServerProcess] = []

    def factory(**settings: object) -> ServerProcess:
        server = start_server(tmp_path / f"server-{len(servers)}", **settings)
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()
        _assert_stdout_is_json_lines(server)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServerProcess]:
    """A shared server with small limits so tests run quickly."""
    process = start_server(
        tmp_path_factory.mktemp("server"),
        IDLE_TIMEOUT_SECONDS=3,
        OUTPUT_BUFFER_MAX_BYTES=MIB,
        MAX_FILES=20,
    )
    yield process
    process.stop()
    _assert_stdout_is_json_lines(process)


def find_tool(*names: str) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    message = f"none of {names} is installed"
    if REQUIRE_ZIP_TOOLS:
        pytest.fail(message)
    pytest.skip(message)


def run_tool(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
