"""Parsing of header parameters: request Content-Type and part Content-Disposition."""

import re
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes

_QUOTED_PAIR = re.compile(r'\\([\\"])')
_EXT_VALUE_CHARSETS = {"utf-8": "utf-8", "iso-8859-1": "latin-1", "us-ascii": "ascii"}


def _split_unquoted(value: str, separator: str) -> list[str]:
    """Split on `separator` outside double-quoted strings."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    index = 0
    while index < len(value):
        char = value[index]
        if in_quotes and char == "\\" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if char == '"':
            in_quotes = not in_quotes
        elif char == separator and not in_quotes:
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # Only `\"` and `\\` are treated as escapes, so unescaped Windows paths sent by some
        # clients (C:\dir\file.txt) keep their separators for the entry name rules.
        return _QUOTED_PAIR.sub(r"\1", value[1:-1])
    return value.strip('"')


def parse_header_params(value: str) -> tuple[str, dict[str, str]]:
    """Parse `main; key=value; key="quoted"` into the lower-cased main value and params.

    Parameter names are lower-cased; the first occurrence of a name wins.
    """
    main, *raw_params = _split_unquoted(value, ";")
    params: dict[str, str] = {}
    for raw in raw_params:
        key, separator, param_value = raw.partition("=")
        key = key.strip().lower()
        if not key or not separator or key in params:
            continue
        params[key] = _unquote(param_value.strip())
    return main.strip().lower(), params


def decode_ext_value(value: str) -> str:
    """Decode an RFC 8187 ext-value (`charset'lang'percent-encoded`), best effort."""
    charset, quote1, rest = value.partition("'")
    _language, quote2, encoded = rest.partition("'")
    if not (quote1 and quote2):
        return value
    codec = _EXT_VALUE_CHARSETS.get(charset.strip().lower(), "utf-8")
    return unquote_to_bytes(encoded).decode(codec, errors="replace")


def decode_header_bytes(raw: bytes) -> str:
    """Header bytes as text: UTF-8 (what browsers send), falling back to Latin-1."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


@dataclass(frozen=True)
class ContentDisposition:
    name: str | None
    # None: no filename parameter (a field part). "": an empty file input (skipped).
    filename: str | None


def parse_content_disposition(value: str) -> ContentDisposition:
    _disposition_type, params = parse_header_params(value)
    filename: str | None = None
    if "filename*" in params:
        filename = decode_ext_value(params["filename*"])
    elif "filename" in params:
        filename = params["filename"]
    return ContentDisposition(name=params.get("name"), filename=filename)


class ContentTypeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def multipart_boundary(content_type: str | None) -> bytes:
    """Return the boundary of a multipart/form-data Content-Type, or raise ContentTypeError."""
    main, params = parse_header_params(content_type or "")
    if main != "multipart/form-data":
        raise ContentTypeError(
            "unsupported-media-type", "The request Content-Type must be multipart/form-data."
        )
    boundary = params.get("boundary", "")
    if not boundary:
        raise ContentTypeError(
            "malformed-request", "The multipart Content-Type is missing its boundary parameter."
        )
    try:
        return boundary.encode("ascii")
    except UnicodeEncodeError:
        raise ContentTypeError(
            "malformed-request", "The multipart boundary must be ASCII."
        ) from None
