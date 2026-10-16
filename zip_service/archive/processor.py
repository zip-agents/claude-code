"""Synchronous multipart-to-zip transformation for one request.

`ArchiveProcessor.feed()` takes raw request body bytes and returns the zip bytes they
produce. It is CPU-bound (multipart scanning, DEFLATE, CRC-32) and touches no I/O, so
the pipeline runs it in a worker thread, one call at a time per request.
"""

import io
import logging
import zipfile
from collections.abc import Buffer
from datetime import datetime
from enum import Enum, auto
from typing import IO, Any, cast

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser

from zip_service.api.problems import ProblemError
from zip_service.archive.multipart import decode_header_bytes, parse_content_disposition
from zip_service.archive.naming import (
    EntryNameDeduplicator,
    InvalidArchiveNameError,
    archive_name,
    sanitize_entry_name,
)

logger = logging.getLogger(__name__)

FILES_FIELD = "files"
ARCHIVE_NAME_FIELD = "filename"
MAX_ARCHIVE_NAME_FIELD_BYTES = 1024
# Part headers are buffered in memory, so they need a cap of their own.
MAX_PART_HEADER_BYTES = 16 * 1024
_UNIX_FILE_MODE = 0o100644


class _Sink(io.RawIOBase):
    """A non-seekable file that collects what zipfile writes, to be handed to the response.

    Being non-seekable makes zipfile stream: it writes sizes and CRCs in a data descriptor
    after each entry instead of seeking back to the local header.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Buffer) -> int:
        view = memoryview(data)
        self._chunks.append(view.tobytes())
        return view.nbytes

    def take(self) -> bytes:
        output = b"".join(self._chunks)
        self._chunks.clear()
        return output


class _StreamingZipFile(zipfile.ZipFile):
    def __del__(self) -> None:
        # ZipFile.__del__ calls close(), which would finish an archive that was abandoned
        # mid-stream (or complain about the open entry). Aborted archives stay truncated.
        pass


class _ZipOutput:
    """Stdlib zipfile driven incrementally: each call returns the zip bytes to send next."""

    def __init__(self, compression_level: int, modified: datetime) -> None:
        self._sink = _Sink()
        self._level = compression_level
        self._method = zipfile.ZIP_DEFLATED if compression_level > 0 else zipfile.ZIP_STORED
        self._date_time = (
            modified.year,
            modified.month,
            modified.day,
            modified.hour,
            modified.minute,
            modified.second,
        )
        self._zip = _StreamingZipFile(cast(IO[bytes], self._sink), "w", self._method)
        self._entry: IO[bytes] | None = None

    def start_entry(self, name: str) -> bytes:
        info = zipfile.ZipInfo(name, date_time=self._date_time)
        info.compress_type = self._method
        info.external_attr = _UNIX_FILE_MODE << 16
        # ZipFile.open() ignores the archive-wide level when given a ZipInfo.
        info._compresslevel = self._level  # type: ignore[attr-defined, unused-ignore]
        # Sizes are unknown until the part ends, so always reserve 64-bit fields: any entry
        # may exceed 4 GiB. zipfile adds Zip64 central-directory records when needed.
        self._entry = self._zip.open(info, "w", force_zip64=True)
        return self._sink.take()

    def write(self, data: bytes) -> bytes:
        assert self._entry is not None
        self._entry.write(data)
        return self._sink.take()

    def end_entry(self) -> bytes:
        assert self._entry is not None
        self._entry.close()
        self._entry = None
        return self._sink.take()

    def finish(self) -> bytes:
        self._zip.close()  # central directory and end records
        return self._sink.take()


class _Mode(Enum):
    DISCARD = auto()
    FILE = auto()
    ARCHIVE_NAME = auto()


class ArchiveProcessor:
    """Parses a multipart body and writes each file part as a zip entry.

    Raises ProblemError for every contract violation. `streaming` turns true when the
    first non-skipped file part starts: from then on the response is committed and any
    error is a mid-stream failure.
    """

    def __init__(
        self, boundary: bytes, *, max_files: int, compression_level: int, modified: datetime
    ) -> None:
        self._max_files = max_files
        self._zip = _ZipOutput(compression_level, modified)
        self._names = EntryNameDeduplicator()
        self._out: list[bytes] = []
        self._mode = _Mode.DISCARD
        self._headers: list[tuple[bytes, bytes]] = []
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._header_bytes = 0
        self._field_value = bytearray()
        self._archive_name_field_seen = False
        self._validated_archive_name: str | None = None
        self._ended = False
        self.archive_name: str | None = None
        self.files_count = 0
        callbacks = {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_end": self._on_end,
        }
        self._parser = MultipartParser(boundary, callbacks=cast(Any, callbacks))

    @property
    def streaming(self) -> bool:
        return self.files_count > 0

    def feed(self, data: bytes) -> bytes:
        try:
            self._parser.write(data)
        except MultipartParseError as exc:
            raise ProblemError("malformed-request") from exc
        return self._take_output()

    def finish(self) -> bytes:
        """Call once the body has ended. Returns the central directory and end records."""
        if not self._ended:
            raise ProblemError(
                "malformed-request", "The request body ended before the closing boundary."
            )
        if self.files_count == 0:
            raise ProblemError("no-files")
        self._out.append(self._zip.finish())
        return self._take_output()

    def _take_output(self) -> bytes:
        output = b"".join(self._out)
        self._out.clear()
        return output

    # --- parser callbacks ---------------------------------------------------------------

    def _on_part_begin(self) -> None:
        self._mode = _Mode.DISCARD
        self._headers = []
        self._header_field.clear()
        self._header_value.clear()
        self._header_bytes = 0
        self._field_value.clear()

    def _count_header_bytes(self, count: int) -> None:
        self._header_bytes += count
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            raise ProblemError("malformed-request", "A multipart part's headers are too large.")

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header_bytes(end - start)
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header_bytes(end - start)
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers.append(
            (bytes(self._header_field).strip().lower(), bytes(self._header_value).strip())
        )
        self._header_field.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        raw = next((value for key, value in self._headers if key == b"content-disposition"), None)
        if raw is None:
            raise ProblemError(
                "malformed-request", "A multipart part has no Content-Disposition header."
            )
        disposition = parse_content_disposition(decode_header_bytes(raw))
        if disposition.filename is None:
            self._begin_field(disposition.name)
        elif disposition.filename:
            self._begin_file(disposition.name, disposition.filename)
        # else: an empty filename is an empty browser file input and is skipped.

    def _begin_field(self, name: str | None) -> None:
        if self.streaming:
            logger.warning(
                "Ignoring field part received after the first file part",
                extra={"fields": {"field": name}},
            )
            return
        if name != ARCHIVE_NAME_FIELD:
            return
        if self._archive_name_field_seen:
            raise ProblemError(
                "invalid-archive-name", "The 'filename' field was sent more than once."
            )
        self._archive_name_field_seen = True
        self._mode = _Mode.ARCHIVE_NAME

    def _begin_file(self, name: str | None, filename: str) -> None:
        if name != FILES_FIELD:
            raise ProblemError(
                "unexpected-file-field",
                f"File parts must use the field name 'files', not {name!r}.",
            )
        if self.files_count >= self._max_files:
            raise ProblemError(
                "too-many-files", f"At most {self._max_files} files are allowed per request."
            )
        if not self.streaming:
            self.archive_name = self._validated_archive_name or archive_name(None)
        self.files_count += 1
        entry_name = self._names.unique(sanitize_entry_name(filename))
        self._out.append(self._zip.start_entry(entry_name))
        self._mode = _Mode.FILE

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._mode is _Mode.FILE:
            self._out.append(self._zip.write(data[start:end]))
        elif self._mode is _Mode.ARCHIVE_NAME:
            self._field_value += data[start:end]
            if len(self._field_value) > MAX_ARCHIVE_NAME_FIELD_BYTES:
                raise ProblemError(
                    "invalid-archive-name",
                    f"The 'filename' field must be at most {MAX_ARCHIVE_NAME_FIELD_BYTES} bytes.",
                )

    def _on_part_end(self) -> None:
        if self._mode is _Mode.FILE:
            self._out.append(self._zip.end_entry())
        elif self._mode is _Mode.ARCHIVE_NAME:
            self._validated_archive_name = self._parse_archive_name(bytes(self._field_value))
        self._mode = _Mode.DISCARD

    def _on_end(self) -> None:
        self._ended = True

    @staticmethod
    def _parse_archive_name(raw: bytes) -> str:
        try:
            return archive_name(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ProblemError(
                "invalid-archive-name", "The 'filename' field must be valid UTF-8."
            ) from None
        except InvalidArchiveNameError as exc:
            raise ProblemError("invalid-archive-name", str(exc)) from None
