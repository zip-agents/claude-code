"""A minimal streaming ZIP writer: every entry uses Zip64 and a data descriptor.

The writer is push-based and side-effect free: each method returns the bytes to emit
next, so callers decide how to buffer and send them. Sizes and CRCs are not known when
an entry starts, so the local header carries zeros and the real values follow the data
in a Zip64 data descriptor and in the central directory (APPNOTE 4.3.9, 4.5.3).
"""

import struct
import zlib
from dataclasses import dataclass
from datetime import datetime

_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_DATA_DESCRIPTOR = struct.Struct("<IIQQ")
_CENTRAL_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
_ZIP64_LOCAL_EXTRA = struct.Struct("<HHQQ")
_ZIP64_CENTRAL_EXTRA = struct.Struct("<HHQQQ")
_ZIP64_END_RECORD = struct.Struct("<IQHHIIQQQQ")
_ZIP64_END_LOCATOR = struct.Struct("<IIQI")
_END_RECORD = struct.Struct("<IHHHHIIH")

_SIG_LOCAL = 0x04034B50
_SIG_DESCRIPTOR = 0x08074B50
_SIG_CENTRAL = 0x02014B50
_SIG_ZIP64_END = 0x06064B50
_SIG_ZIP64_LOCATOR = 0x07064B50
_SIG_END = 0x06054B50

_ZIP64_EXTRA_ID = 0x0001
_VERSION_ZIP64 = 45
_VERSION_MADE_BY = (3 << 8) | _VERSION_ZIP64  # Unix host, so external attributes are modes
_FLAG_DATA_DESCRIPTOR = 0x0008
_FLAG_UTF8 = 0x0800
_METHOD_STORED = 0
_METHOD_DEFLATED = 8
_UNIX_FILE_MODE = 0o100644
_MAX32 = 0xFFFFFFFF
_MAX16 = 0xFFFF


def dos_datetime(moment: datetime) -> tuple[int, int]:
    """(time, date) in MS-DOS format. DOS dates cannot represent years before 1980."""
    if moment.year < 1980:
        return 0, (1 << 5) | 1
    time = (moment.hour << 11) | (moment.minute << 5) | (moment.second // 2)
    date = ((moment.year - 1980) << 9) | (moment.month << 5) | moment.day
    return time, date


@dataclass
class _Entry:
    name: bytes
    flags: int
    offset: int
    crc: int = 0
    size: int = 0
    compressed_size: int = 0


class ZipStreamWriter:
    def __init__(self, compression_level: int, modified: datetime) -> None:
        self._level = compression_level
        self._method = _METHOD_DEFLATED if compression_level > 0 else _METHOD_STORED
        self._time, self._date = dos_datetime(modified)
        self._offset = 0
        self._entries: list[_Entry] = []
        self._current: _Entry | None = None
        self._compressor: zlib._Compress | None = None

    def _emitted(self, data: bytes) -> bytes:
        self._offset += len(data)
        return data

    def start_entry(self, name: str) -> bytes:
        if self._current is not None:
            raise RuntimeError("previous entry was not ended")
        encoded = name.encode("utf-8")
        flags = _FLAG_DATA_DESCRIPTOR | (0 if name.isascii() else _FLAG_UTF8)
        self._current = _Entry(name=encoded, flags=flags, offset=self._offset)
        if self._method == _METHOD_DEFLATED:
            self._compressor = zlib.compressobj(self._level, zlib.DEFLATED, -zlib.MAX_WBITS)
        extra = _ZIP64_LOCAL_EXTRA.pack(_ZIP64_EXTRA_ID, 16, 0, 0)
        header = _LOCAL_HEADER.pack(
            _SIG_LOCAL,
            _VERSION_ZIP64,
            flags,
            self._method,
            self._time,
            self._date,
            0,
            _MAX32,
            _MAX32,
            len(encoded),
            len(extra),
        )
        return self._emitted(header + encoded + extra)

    def write(self, data: bytes) -> bytes:
        entry = self._require_entry()
        entry.crc = zlib.crc32(data, entry.crc)
        entry.size += len(data)
        output = self._compressor.compress(data) if self._compressor else bytes(data)
        entry.compressed_size += len(output)
        return self._emitted(output)

    def end_entry(self) -> bytes:
        entry = self._require_entry()
        tail = self._compressor.flush() if self._compressor else b""
        entry.compressed_size += len(tail)
        descriptor = _DATA_DESCRIPTOR.pack(
            _SIG_DESCRIPTOR, entry.crc, entry.compressed_size, entry.size
        )
        self._entries.append(entry)
        self._current = None
        self._compressor = None
        return self._emitted(tail + descriptor)

    def finish(self) -> bytes:
        """Central directory plus the Zip64 and classic end records."""
        if self._current is not None:
            raise RuntimeError("an entry is still open")
        directory_offset = self._offset
        records: list[bytes] = []
        for entry in self._entries:
            extra = _ZIP64_CENTRAL_EXTRA.pack(
                _ZIP64_EXTRA_ID, 24, entry.size, entry.compressed_size, entry.offset
            )
            header = _CENTRAL_HEADER.pack(
                _SIG_CENTRAL,
                _VERSION_MADE_BY,
                _VERSION_ZIP64,
                entry.flags,
                self._method,
                self._time,
                self._date,
                entry.crc,
                _MAX32,
                _MAX32,
                len(entry.name),
                len(extra),
                0,  # comment length
                0,  # disk number start
                0,  # internal attributes
                _UNIX_FILE_MODE << 16,
                _MAX32,
            )
            records.append(header + entry.name + extra)
        directory = b"".join(records)
        zip64_end_offset = directory_offset + len(directory)
        count = len(self._entries)
        trailer = (
            _ZIP64_END_RECORD.pack(
                _SIG_ZIP64_END,
                _ZIP64_END_RECORD.size - 12,
                _VERSION_MADE_BY,
                _VERSION_ZIP64,
                0,
                0,
                count,
                count,
                len(directory),
                directory_offset,
            )
            + _ZIP64_END_LOCATOR.pack(_SIG_ZIP64_LOCATOR, 0, zip64_end_offset, 1)
            + _END_RECORD.pack(_SIG_END, 0, 0, _MAX16, _MAX16, _MAX32, _MAX32, 0)
        )
        return self._emitted(directory + trailer)

    def _require_entry(self) -> _Entry:
        if self._current is None:
            raise RuntimeError("no entry has been started")
        return self._current
