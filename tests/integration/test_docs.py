"""Docs endpoints (contract §14) against a real server process."""

from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import ServerFactory

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_every_listed_document_is_served_byte_for_byte(make_server: ServerFactory) -> None:
    # The server runs in a temporary working directory: documents are found next to
    # main.py, not relative to the cwd.
    server = make_server(GIT_SHA="0123456789abcdef")
    manifest = httpx.get(f"{server.url}/docs/manifest.json", timeout=10).json()
    assert manifest["revision"] == "0123456789abcdef"
    assert manifest["contract_version"] == "1.1.0"

    for document in manifest["documents"]:
        response = httpx.get(f"{server.url}{document['path']}", timeout=10)
        assert response.status_code == 200, document
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        expected = (PROJECT_ROOT / Path(document["path"]).name).read_bytes()
        assert response.content == expected

    missing = httpx.get(f"{server.url}/docs/missing.md", timeout=10)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not-found"
