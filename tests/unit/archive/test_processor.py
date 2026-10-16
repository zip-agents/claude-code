import io
import logging
import zipfile
from datetime import UTC, datetime

import pytest

from tests.multipart_builder import BOUNDARY, Part, body, field_part, file_part
from zip_service.api.problems import ProblemError
from zip_service.archive.processor import ArchiveProcessor

NOW = datetime(2025, 1, 2, 3, 4, 6, tzinfo=UTC)


def _processor(max_files: int = 500, level: int = 6) -> ArchiveProcessor:
    return ArchiveProcessor(
        BOUNDARY.encode(), max_files=max_files, compression_level=level, modified=NOW
    )


def _run(
    payload: bytes, chunk_size: int | None = None, **kwargs: int
) -> tuple[ArchiveProcessor, bytes]:
    processor = _processor(**kwargs)
    out = bytearray()
    step = chunk_size or len(payload) or 1
    for offset in range(0, len(payload), step):
        out += processor.feed(payload[offset : offset + step])
    out += processor.finish()
    return processor, bytes(out)


def _zip(data: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    return archive


@pytest.mark.parametrize("chunk_size", [None, 1, 7, 4096])
def test_contract_example_in_any_chunking(chunk_size: int | None) -> None:
    payload = body(
        [
            field_part("filename", "quarterly report"),
            file_part("a.txt", b"first"),
            file_part("a.txt", b"second"),
            file_part("photo.jpg", bytes(range(256)) * 50),
        ]
    )
    processor, data = _run(payload, chunk_size)
    assert processor.archive_name == "quarterly report.zip"
    assert processor.files_count == 3
    archive = _zip(data)
    assert archive.namelist() == ["a.txt", "a (1).txt", "photo.jpg"]
    assert archive.read("a.txt") == b"first"
    assert archive.read("a (1).txt") == b"second"
    assert archive.read("photo.jpg") == bytes(range(256)) * 50


def test_default_archive_name_and_stored_level() -> None:
    processor, data = _run(body([file_part("x.bin", b"\x00" * 10)]), level=0)
    assert processor.archive_name == "zipped_files.zip"
    assert _zip(data).getinfo("x.bin").compress_type == zipfile.ZIP_STORED


def test_empty_filenames_are_skipped_and_zero_byte_files_kept() -> None:
    payload = body([file_part("", b"ignored"), file_part("empty.txt", b""), file_part("", b"")])
    processor, data = _run(payload)
    assert processor.files_count == 1
    archive = _zip(data)
    assert archive.namelist() == ["empty.txt"]
    assert archive.read("empty.txt") == b""


def test_entry_names_are_sanitised() -> None:
    payload = body([file_part("../../etc/passwd", b"x"), file_part("C:\\tmp\\a.txt", b"y")])
    _, data = _run(payload)
    assert _zip(data).namelist() == ["passwd", "a.txt"]


def test_filename_star_is_preferred() -> None:
    part = Part(
        'form-data; name="files"; filename="plain.txt"; filename*=UTF-8\'\'%E2%82%AC.txt',
        [b"euro"],
    )
    _, data = _run(body([part]))
    archive = _zip(data)
    assert archive.namelist() == ["\u20ac.txt"]
    assert archive.getinfo("\u20ac.txt").flag_bits & 0x800


def test_other_fields_before_files_are_ignored() -> None:
    payload = body([field_part("comment", "hi"), file_part("a.txt", b"a")])
    processor, _ = _run(payload)
    assert processor.files_count == 1


def test_fields_after_first_file_are_ignored_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    payload = body([file_part("a.txt", b"a"), field_part("filename", "late"), field_part("x", "y")])
    with caplog.at_level(logging.WARNING):
        processor, data = _run(payload)
    assert processor.archive_name == "zipped_files.zip"
    assert _zip(data).namelist() == ["a.txt"]
    assert sum("after the first file part" in r.getMessage() for r in caplog.records) == 2


def _error(payload: bytes, **kwargs: int) -> tuple[ProblemError, ArchiveProcessor]:
    processor = _processor(**kwargs)

    def feed_all() -> None:
        processor.feed(payload)
        processor.finish()

    with pytest.raises(ProblemError) as excinfo:
        feed_all()
    return excinfo.value, processor


@pytest.mark.parametrize(
    ("parts", "code"),
    [
        ([field_part("comment", "x")], "no-files"),
        ([file_part("", b"x")], "no-files"),
        ([file_part("a.txt", b"x", name="file")], "unexpected-file-field"),
        ([field_part("filename", "a"), field_part("filename", "b")], "invalid-archive-name"),
        ([field_part("filename", "x" * 1025)], "invalid-archive-name"),
        ([field_part("filename", "a/b")], "invalid-archive-name"),
        ([field_part("filename", b"\xff\xfe")], "invalid-archive-name"),
        ([field_part("filename", "x" * 300)], "invalid-archive-name"),
    ],
)
def test_errors_before_streaming(parts: list[Part], code: str) -> None:
    error, processor = _error(body(parts))
    assert error.code == code
    assert not processor.streaming


def test_filename_field_of_exactly_1024_bytes_is_measured_not_rejected() -> None:
    # 1024 bytes is allowed by §2.2 but then fails the 255-byte rule of §2.3.
    error, _ = _error(body([field_part("filename", "x" * 1024), file_part("a", b"")]))
    assert error.code == "invalid-archive-name"
    assert "255" in error.detail


@pytest.mark.parametrize(
    "payload",
    [
        b"this is not multipart",
        body([file_part("a.txt", b"x")], close=False),  # no closing boundary
        b"",
        (
            f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nno disposition\r\n--{BOUNDARY}--\r\n"
        ).encode(),
    ],
)
def test_malformed_bodies(payload: bytes) -> None:
    error, _ = _error(payload)
    assert error.code == "malformed-request"


def test_oversized_part_headers_are_rejected() -> None:
    part = Part('form-data; name="x"; junk="' + "j" * 20_000 + '"', [b""])
    error, _ = _error(body([part]))
    assert error.code == "malformed-request"


def test_later_wrong_file_field_is_a_mid_stream_error() -> None:
    error, processor = _error(body([file_part("a", b"1"), file_part("b", b"2", name="other")]))
    assert error.code == "unexpected-file-field"
    assert processor.streaming


def test_too_many_files_is_a_mid_stream_error() -> None:
    payload = body([file_part("a", b"1"), file_part("b", b"2"), file_part("c", b"3")])
    error, processor = _error(payload, max_files=2)
    assert error.code == "too-many-files"
    assert processor.streaming
    assert processor.files_count == 2


def test_skipped_parts_do_not_count_towards_max_files() -> None:
    payload = body([file_part("", b""), file_part("a", b"1"), file_part("", b""), file_part("b")])
    processor, _ = _run(payload, max_files=2)
    assert processor.files_count == 2


def test_streaming_starts_at_first_file_part() -> None:
    processor = _processor()
    head = body([field_part("filename", "n")], close=False)
    assert processor.feed(head) == b""
    assert not processor.streaming
    output = processor.feed(file_part("a.txt").head())
    assert processor.streaming
    assert output.startswith(b"PK\x03\x04")
