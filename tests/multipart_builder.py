"""Helpers to build multipart/form-data bodies in tests (shared by unit and integration)."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

BOUNDARY = "zipServiceTestBoundary7MA4YWxkTrZu0gW"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


@dataclass
class Part:
    disposition: str
    data: Iterable[bytes] = field(default_factory=list)
    content_type: str | None = None

    def head(self, boundary: str = BOUNDARY) -> bytes:
        lines = [f"--{boundary}", f"Content-Disposition: {self.disposition}"]
        if self.content_type:
            lines.append(f"Content-Type: {self.content_type}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def file_part(filename: str, data: bytes | Iterable[bytes] = b"", name: str = "files") -> Part:
    chunks = [data] if isinstance(data, bytes) else data
    return Part(
        f'form-data; name="{name}"; filename="{filename}"', chunks, "application/octet-stream"
    )


def field_part(name: str, value: str | bytes) -> Part:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return Part(f'form-data; name="{name}"', [raw])


def iter_body(parts: Iterable[Part], boundary: str = BOUNDARY, close: bool = True) -> Iterator[bytes]:
    for part in parts:
        yield part.head(boundary)
        yield from part.data
        yield b"\r\n"
    if close:
        yield f"--{boundary}--\r\n".encode()


def body(parts: Iterable[Part], boundary: str = BOUNDARY, close: bool = True) -> bytes:
    return b"".join(iter_body(parts, boundary, close))
