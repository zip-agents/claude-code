import io
import struct
import zipfile
from datetime import UTC, datetime

import pytest

from zip_service.zipwriter import ZipStreamWriter

MODIFIED = datetime(2024, 5, 17, 13, 45, 31, tzinfo=UTC)


def _build(entries: list[tuple[str, list[bytes]]], level: int) -> bytes:
    writer = ZipStreamWriter(level, MODIFIED)
    out = bytearray()
    for name, chunks in entries:
        out += writer.start_entry(name)
        for chunk in chunks:
            out += writer.write(chunk)
        out += writer.end_entry()
    out += writer.finish()
    return bytes(out)


ENTRIES = [
    ("hello.txt", [b"hello ", b"world\n" * 1000]),
    ("empty.bin", []),
    ("café 文件.txt", [b"unicode name"]),
    ("random.bin", [bytes(range(256)) * 300]),
]


@pytest.mark.parametrize("level", [0, 1, 6, 9])
def test_roundtrip_with_zipfile(level: int) -> None:
    archive = zipfile.ZipFile(io.BytesIO(_build(ENTRIES, level)))
    assert archive.testzip() is None
    assert archive.namelist() == [name for name, _ in ENTRIES]
    for name, chunks in ENTRIES:
        assert archive.read(name) == b"".join(chunks)
    expected_method = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
    for info in archive.infolist():
        assert info.compress_type == expected_method
        assert info.date_time == (2024, 5, 17, 13, 45, 30)  # DOS time has 2 s resolution


def test_every_central_entry_has_zip64_extra_and_correct_utf8_flag() -> None:
    archive = zipfile.ZipFile(io.BytesIO(_build(ENTRIES, 6)))
    for info in archive.infolist():
        header_id, size = struct.unpack_from("<HH", info.extra)
        assert (header_id, size) == (0x0001, 24)
        assert info.flag_bits & 0x0008  # data descriptor
        assert bool(info.flag_bits & 0x0800) == (not info.filename.isascii())
        assert info.create_version == 45
        assert info.extract_version == 45


def test_local_headers_carry_zip64_extra() -> None:
    data = _build([("a.txt", [b"abc"])], 6)
    signature, version, flags = struct.unpack_from("<IHH", data)
    assert signature == 0x04034B50
    assert version == 45
    compressed, uncompressed, name_len, extra_len = struct.unpack_from("<IIHH", data, 18)
    assert (compressed, uncompressed) == (0xFFFFFFFF, 0xFFFFFFFF)
    extra_id, extra_size = struct.unpack_from("<HH", data, 30 + name_len)
    assert (extra_id, extra_size, extra_len) == (0x0001, 16, 20)


def test_archive_ends_with_zip64_end_records() -> None:
    data = _build([("a.txt", [b"abc"])], 6)
    assert data[-22:-18] == struct.pack("<I", 0x06054B50)
    assert data[-42:-38] == struct.pack("<I", 0x07064B50)
    assert data[-98:-94] == struct.pack("<I", 0x06064B50)


def test_empty_archive_is_valid() -> None:
    archive = zipfile.ZipFile(io.BytesIO(_build([], 6)))
    assert archive.namelist() == []


def test_output_is_produced_incrementally() -> None:
    writer = ZipStreamWriter(0, MODIFIED)
    assert writer.start_entry("a")
    assert writer.write(b"x" * 1000) == b"x" * 1000  # STORED passes data straight through


def test_misuse_raises() -> None:
    writer = ZipStreamWriter(6, MODIFIED)
    with pytest.raises(RuntimeError):
        writer.write(b"x")
    writer.start_entry("a")
    with pytest.raises(RuntimeError):
        writer.start_entry("b")
    with pytest.raises(RuntimeError):
        writer.finish()
