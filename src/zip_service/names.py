"""Archive and entry naming rules (contract §2.3, §2.4) and Content-Disposition encoding (§3)."""

import unicodedata
from urllib.parse import quote

DEFAULT_ARCHIVE_NAME = "zipped_files.zip"
DEFAULT_ENTRY_NAME = "file"
MAX_NAME_BYTES = 255
MAX_PRESERVED_EXTENSION_BYTES = 16

# RFC 8187 attr-char beyond ALPHA / DIGIT; `quote` always keeps ALPHA, DIGIT and "_.-~".
_ATTR_CHAR_SAFE = "!#$&+^`|"


class InvalidArchiveNameError(ValueError):
    """The requested archive name breaks a rule in contract §2.3."""


def _is_control(char: str) -> bool:
    code = ord(char)
    return code <= 0x1F or code == 0x7F


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate to at most `max_bytes` of UTF-8 without splitting a character."""
    return text.encode("utf-8")[: max(max_bytes, 0)].decode("utf-8", errors="ignore")


def split_extension(name: str) -> tuple[str, str]:
    """Split at the last dot. A leading dot does not start an extension (`.env` has none)."""
    index = name.rfind(".")
    if index <= 0:
        return name, ""
    return name[:index], name[index:]


def archive_name(raw: str | None) -> str:
    """Apply the archive name rules; raise InvalidArchiveNameError when they reject it."""
    if raw is None or not raw.strip():
        return DEFAULT_ARCHIVE_NAME
    # Checked before trimming so trailing control characters (e.g. "\n") are rejected,
    # not silently trimmed away.
    if any(_is_control(char) or char in "/\\" for char in raw):
        raise InvalidArchiveNameError(
            "The archive name must not contain control characters, '/' or '\\'."
        )
    name = unicodedata.normalize("NFC", raw.strip())
    if not name.lower().endswith(".zip"):
        name += ".zip"
    if _utf8_len(name) > MAX_NAME_BYTES:
        raise InvalidArchiveNameError(
            f"The archive name must be at most {MAX_NAME_BYTES} bytes in UTF-8."
        )
    return name


def _truncate_name(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    if _utf8_len(name) <= max_bytes:
        return name
    stem, extension = split_extension(name)
    extension_bytes = _utf8_len(extension)
    if extension and extension_bytes <= MAX_PRESERVED_EXTENSION_BYTES:
        return _truncate_utf8(stem, max_bytes - extension_bytes) + extension
    return _truncate_utf8(name, max_bytes)


def sanitize_entry_name(raw: str) -> str:
    """Turn an uploaded filename into a safe, flat entry name. Never rejects."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if not _is_control(char))
    name = unicodedata.normalize("NFC", name.strip())
    if name in ("", ".", ".."):
        name = DEFAULT_ENTRY_NAME
    return _truncate_name(name)


def _collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


class EntryNameDeduplicator:
    """Makes entry names unique case-insensitively by inserting ` (n)` before the extension."""

    def __init__(self) -> None:
        self._taken: set[str] = set()
        # Lowest suffix still worth trying per original name. Names are never released, so
        # every smaller suffix was already found taken; this keeps repeats linear.
        self._next_suffix: dict[str, int] = {}

    def unique(self, name: str) -> str:
        key = _collision_key(name)
        if key not in self._taken:
            self._taken.add(key)
            return name
        stem, extension = split_extension(name)
        suffix = self._next_suffix.get(key, 1)
        while True:
            marker = f" ({suffix}){extension}"
            candidate = _truncate_utf8(stem, MAX_NAME_BYTES - _utf8_len(marker)) + marker
            suffix += 1
            candidate_key = _collision_key(candidate)
            if candidate_key not in self._taken:
                self._taken.add(candidate_key)
                self._next_suffix[key] = suffix
                return candidate


def content_disposition(name: str) -> str:
    """`attachment` header with an ASCII fallback and an RFC 8187 UTF-8 `filename*`."""
    fallback = "".join("_" if ord(char) > 0x7F or char in '"\\' else char for char in name)
    encoded = quote(name, safe=_ATTR_CHAR_SAFE, encoding="utf-8")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
