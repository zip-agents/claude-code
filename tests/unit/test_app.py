"""In-process tests of routing and error handling. Streaming behaviour is covered against a
real server in tests/integration."""

import io
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.multipart_builder import CONTENT_TYPE, body, field_part, file_part
from zip_service import app as app_module
from zip_service.app import create_app
from zip_service.config import Settings
from zip_service.lifecycle import InflightTracker

ZIP_URL = "/api/v1/zip"


@pytest.fixture
def tracker() -> InflightTracker:
    return InflightTracker(limit=2)


@pytest.fixture
def client(tracker: InflightTracker) -> TestClient:
    settings = Settings(max_concurrent_requests=2, idle_timeout_seconds=5)
    return TestClient(create_app(settings, tracker), raise_server_exceptions=False)


def _assert_problem(response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    problem: dict[str, object] = response.json()
    assert problem["status"] == status
    assert problem["code"] == code
    assert problem["type"] == f"urn:zip-service:problem:{code}"
    assert problem["title"]
    assert problem["detail"]
    assert problem["request_id"] == response.headers["x-request-id"]
    return problem


def test_health_endpoints(client: TestClient, tracker: InflightTracker) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    tracker.begin_shutdown()
    ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "shutting-down"}
    assert client.get("/health/live").status_code == 200


def test_zip_happy_path(client: TestClient) -> None:
    payload = body([field_part("filename", "report"), file_part("a.txt", b"hello")])
    response = client.post(ZIP_URL, content=payload, headers={"content-type": CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"report.zip\"; filename*=UTF-8''report.zip"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-length" not in response.headers
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.read("a.txt") == b"hello"


def test_request_id_is_echoed_when_valid(client: TestClient) -> None:
    response = client.get("/health/live", headers={"x-request-id": "abc.DEF_123-x"})
    assert response.headers["x-request-id"] == "abc.DEF_123-x"


@pytest.mark.parametrize("bad_id", ["has space", "x" * 129, "semi;colon", ""])
def test_invalid_request_id_is_replaced_with_uuid4(client: TestClient, bad_id: str) -> None:
    response = client.get("/health/live", headers={"x-request-id": bad_id})
    assert uuid.UUID(response.headers["x-request-id"]).version == 4


def test_not_found_and_method_not_allowed_are_problems(client: TestClient) -> None:
    _assert_problem(client.get("/nope"), 404, "not-found")
    _assert_problem(client.get(ZIP_URL), 405, "method-not-allowed")
    _assert_problem(client.post("/health/live"), 405, "method-not-allowed")


def test_unsupported_media_type(client: TestClient) -> None:
    response = client.post(ZIP_URL, content=b"{}", headers={"content-type": "application/json"})
    _assert_problem(response, 415, "unsupported-media-type")


def test_missing_boundary(client: TestClient) -> None:
    response = client.post(ZIP_URL, content=b"x", headers={"content-type": "multipart/form-data"})
    _assert_problem(response, 400, "malformed-request")


def test_no_files(client: TestClient) -> None:
    payload = body([field_part("other", "x")])
    response = client.post(ZIP_URL, content=payload, headers={"content-type": CONTENT_TYPE})
    _assert_problem(response, 400, "no-files")


def test_overloaded_has_retry_after(client: TestClient, tracker: InflightTracker) -> None:
    assert tracker.try_acquire()
    assert tracker.try_acquire()
    response = client.post(ZIP_URL, content=b"", headers={"content-type": CONTENT_TYPE})
    _assert_problem(response, 503, "overloaded")
    assert response.headers["retry-after"] == "5"
    # Health checks are excluded from the concurrency limit.
    assert client.get("/health/live").status_code == 200


def test_slot_is_released_after_rejection(client: TestClient, tracker: InflightTracker) -> None:
    client.post(ZIP_URL, content=b"{}", headers={"content-type": "application/json"})
    client.post(ZIP_URL, content=body([]), headers={"content-type": CONTENT_TYPE})
    assert tracker.inflight == 0


def test_shutting_down(client: TestClient, tracker: InflightTracker) -> None:
    tracker.begin_shutdown()
    response = client.post(ZIP_URL, content=b"", headers={"content-type": CONTENT_TYPE})
    _assert_problem(response, 503, "shutting-down")


def test_internal_error_is_a_problem_without_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_: str | None) -> bytes:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app_module, "multipart_boundary", explode)
    response = client.post(ZIP_URL, content=b"", headers={"content-type": CONTENT_TYPE})
    problem = _assert_problem(response, 500, "internal-error")
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert problem["detail"]
