"""Archives produced by the real server are valid for zipfile, unzip and 7z (contract §2.5)."""

import hashlib
import zipfile
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import ServerFactory, find_tool, run_tool
from tests.integration.support import MIB, ServerProcess, random_chunks
from tests.multipart_builder import CONTENT_TYPE, body, field_part, file_part

pytestmark = pytest.mark.slow

UPLOADS: list[tuple[str, bytes]] = [
    ("a.txt", b"alpha\n" * 1000),
    ("a.txt", b"second a"),
    ("A.TXT", b"third, case-insensitive duplicate"),
    ("empty.bin", b""),
    ("dir/nested/résumé 文件.txt", "unicode content ✓".encode()),
    ("random.bin", b"".join(random_chunks(3 * MIB, seed=1))),
    (".env", b"SECRET=nope"),
]
EXPECTED_NAMES = [
    "a.txt",
    "a (1).txt",
    "A (2).TXT",
    "empty.bin",
    "résumé 文件.txt",
    "random.bin",
    ".env",
]


def _fetch_archive(server: ServerProcess, tmp_path: Path) -> Path:
    payload = body(
        [field_part("filename", "validity test")]
        + [file_part(name, data) for name, data in UPLOADS]
    )
    response = httpx.post(
        f"{server.url}/api/v1/zip",
        content=payload,
        headers={"content-type": CONTENT_TYPE},
        timeout=60,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in response.headers
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"validity test.zip\"; filename*=UTF-8''validity%20test.zip"
    )
    path = tmp_path / "out.zip"
    path.write_bytes(response.content)
    return path


@pytest.fixture(params=[0, 6], ids=["stored", "deflate"])
def archive_path(request: pytest.FixtureRequest, make_server: ServerFactory, tmp_path: Path) -> Path:
    server = make_server(ZIP_COMPRESSION_LEVEL=request.param)
    return _fetch_archive(server, tmp_path)


def test_python_zipfile_contents_match_byte_for_byte(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == EXPECTED_NAMES
        for name, (_, data) in zip(EXPECTED_NAMES, UPLOADS, strict=True):
            assert hashlib.sha256(archive.read(name)).digest() == hashlib.sha256(data).digest()
        timestamps = {info.date_time for info in archive.infolist()}
        assert len(timestamps) == 1


def test_unzip_t(archive_path: Path) -> None:
    unzip = find_tool("unzip")
    result = run_tool([unzip, "-t", str(archive_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No errors detected" in result.stdout


def test_7z_t(archive_path: Path) -> None:
    seven_zip = find_tool("7z", "7zz", "7za")
    result = run_tool([seven_zip, "t", str(archive_path)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Everything is Ok" in result.stdout


def test_unzip_extracts_identical_bytes(archive_path: Path, tmp_path: Path) -> None:
    unzip = find_tool("unzip")
    target = tmp_path / "extracted"
    result = run_tool([unzip, "-q", str(archive_path), "-d", str(target)])
    assert result.returncode == 0, result.stdout + result.stderr
    for name, (_, data) in zip(EXPECTED_NAMES, UPLOADS, strict=True):
        assert (target / name).read_bytes() == data


def test_contract_example(server: ServerProcess, tmp_path: Path) -> None:
    payload = body(
        [
            field_part("filename", "quarterly report"),
            file_part("a.txt", b"a"),
            file_part("a.txt", b"a"),
            file_part("photo.jpg", b"\xff\xd8\xff"),
        ]
    )
    response = httpx.post(
        f"{server.url}/api/v1/zip", content=payload, headers={"content-type": CONTENT_TYPE}
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"quarterly report.zip\"; "
        "filename*=UTF-8''quarterly%20report.zip"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"]
    path = tmp_path / "example.zip"
    path.write_bytes(response.content)
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["a.txt", "a (1).txt", "photo.jpg"]
