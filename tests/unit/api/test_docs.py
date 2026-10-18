"""Docs endpoints (contract §14), in process."""

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from zip_service import __version__
from zip_service.api.docs import DOCS_ROOT, MAX_DOCUMENT_BYTES, DocsCatalog, DocsError
from zip_service.api.problems import PROBLEMS
from zip_service.app import create_app
from zip_service.config import Settings
from zip_service.server.lifecycle import InflightTracker

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def client() -> TestClient:
    docs = DocsCatalog.load(revision="3f2c9e1")
    return TestClient(create_app(Settings(), docs=docs), raise_server_exceptions=False)


def test_manifest(client: TestClient) -> None:
    response = client.get("/docs/manifest.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.json() == {
        "service": "zip-service",
        "version": __version__,
        "revision": "3f2c9e1",
        "contract_version": "1.1.0",
        "openapi": "/docs/openapi.json",
        "documents": [
            {"slug": "readme", "title": "README", "path": "/docs/README.md"},
            {"slug": "agents", "title": "AGENTS", "path": "/docs/AGENTS.md"},
        ],
    }


@pytest.mark.parametrize("name", ["README.md", "AGENTS.md"])
def test_documents_are_served_byte_for_byte(client: TestClient, name: str) -> None:
    response = client.get(f"/docs/{name}")
    assert response.status_code == 200
    assert response.content == (PROJECT_ROOT / name).read_bytes()
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize(
    "path",
    [
        "/docs/nope.md",
        "/docs/readme.md",  # names are exact, not case-insensitive
        "/docs/PLAN.md",  # exists in the repository, but is not in the allow-list
        "/docs/main.py",
        "/docs/..%2Fmain.py",
        "/docs/%2e%2e/main.py",
        "/docs/sub/README.md",
        "/docs",
    ],
)
def test_anything_else_is_a_not_found_problem(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not-found"


def test_wrong_method_is_a_problem(client: TestClient) -> None:
    response = client.post("/docs/README.md")
    assert response.status_code == 405
    assert response.json()["code"] == "method-not-allowed"


def test_docs_stay_available_during_shutdown() -> None:
    tracker = InflightTracker(limit=1)
    app = create_app(Settings(), tracker, DocsCatalog.load(revision="x"))
    client = TestClient(app, raise_server_exceptions=False)
    tracker.begin_shutdown()
    assert client.get("/docs/manifest.json").status_code == 200


def test_revision_comes_from_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "feedface")
    assert json.loads(DocsCatalog.load().manifest)["revision"] == "feedface"
    monkeypatch.delenv("GIT_SHA")
    assert json.loads(DocsCatalog.load().manifest)["revision"] == "unknown"


def test_missing_document_fails_at_startup(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# readme\n")
    with pytest.raises(DocsError, match=r"AGENTS\.md"):
        DocsCatalog.load(tmp_path)


def test_oversized_document_fails_at_startup(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    (tmp_path / "AGENTS.md").write_text("# agents\n")
    with pytest.raises(DocsError, match=r"README\.md"):
        DocsCatalog.load(tmp_path)


def test_shipped_documents_fit_the_limit() -> None:
    for name in ("README.md", "AGENTS.md"):
        assert (DOCS_ROOT / name).stat().st_size <= MAX_DOCUMENT_BYTES


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


# --- the generated OpenAPI document (/docs/openapi.json) ------------------------------------


@pytest.fixture
def spec(client: TestClient) -> dict[str, Any]:
    response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document


def test_spec_lists_every_route(spec: dict[str, Any]) -> None:
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["version"] == __version__
    assert {path: set(ops) for path, ops in spec["paths"].items()} == {
        "/api/v1/zip": {"post"},
        "/health/live": {"get"},
        "/health/ready": {"get"},
        "/docs/manifest.json": {"get"},
        "/docs/{name}": {"get"},
    }
    # Describes the service as reached directly: a gateway adds servers and auth.
    assert "servers" not in spec
    assert "security" not in spec


def test_spec_describes_the_zip_request_body(spec: dict[str, Any]) -> None:
    body = spec["paths"]["/api/v1/zip"]["post"]["requestBody"]["content"]
    schema = body["multipart/form-data"]["schema"]
    assert schema["required"] == ["files"]
    assert list(schema["properties"]) == ["filename", "files"]  # the order clients must send
    files = schema["properties"]["files"]
    assert files["type"] == "array"
    assert files["items"] == {"type": "string", "format": "binary"}
    assert files["maxItems"] == Settings().max_files


def test_zip_errors_are_generated_from_the_problem_table(spec: dict[str, Any]) -> None:
    responses = spec["paths"]["/api/v1/zip"]["post"]["responses"]
    assert set(responses) == {"200", "400", "408", "415", "422", "500", "503"}
    assert "application/zip" in responses["200"]["content"]
    documented = {
        code
        for status, response in responses.items()
        if status != "200"
        for code in response["content"]["application/problem+json"]["examples"]
    }
    # Every problem a zip request can produce before streaming. 404/405 belong to unknown
    # paths and methods; too-many-files only ever aborts mid-stream (see the README).
    assert documented == {
        code
        for code, problem in PROBLEMS.items()
        if problem.status not in (404, 405) and code != "too-many-files"
    }
    for code in documented:
        status = str(PROBLEMS[code].status)
        example = responses[status]["content"]["application/problem+json"]["examples"][code]
        assert example["value"]["code"] == code
        assert example["value"]["status"] == PROBLEMS[code].status
    assert "Retry-After" in responses["503"]["headers"]


def test_docs_route_documents_no_validation_error(spec: dict[str, Any]) -> None:
    operation = spec["paths"]["/docs/{name}"]["get"]
    assert set(operation["responses"]) == {"200", "404"}
    assert operation["parameters"][0]["schema"]["enum"] == ["README.md", "AGENTS.md"]


def test_every_ref_resolves(spec: dict[str, Any]) -> None:
    def refs(node: object) -> list[str]:
        if isinstance(node, dict):
            found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
            return found + [ref for value in node.values() for ref in refs(value)]
        if isinstance(node, list):
            return [ref for value in node for ref in refs(value)]
        return []

    all_refs = refs(spec)
    assert "#/components/schemas/Problem" in all_refs
    for ref in all_refs:
        target: Any = spec
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]  # KeyError: a dangling reference


def test_spec_ui_pages_stay_disabled(client: TestClient) -> None:
    for path in ("/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404
