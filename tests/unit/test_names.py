import unicodedata

import pytest

from zip_service.names import (
    EntryNameDeduplicator,
    InvalidArchiveNameError,
    archive_name,
    content_disposition,
    sanitize_entry_name,
    split_extension,
)

# --- archive names (§2.3) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "zipped_files.zip"),
        ("", "zipped_files.zip"),
        ("   ", "zipped_files.zip"),
        ("report", "report.zip"),
        ("report.zip", "report.zip"),
        ("report.ZIP", "report.ZIP"),
        ("  quarterly report  ", "quarterly report.zip"),
        ("report.tar", "report.tar.zip"),
        ("résumé", "résumé.zip"),
        (".zip", ".zip"),
    ],
)
def test_archive_name_valid(raw: str | None, expected: str) -> None:
    assert archive_name(raw) == expected


def test_archive_name_is_nfc_normalised() -> None:
    decomposed = unicodedata.normalize("NFD", "café")
    assert archive_name(decomposed) == unicodedata.normalize("NFC", "café.zip")


@pytest.mark.parametrize(
    "raw",
    ["a/b", "a\\b", "bad\x00name", "tab\there", "line\n", "\x7f", "x" * 252],
)
def test_archive_name_rejected(raw: str) -> None:
    with pytest.raises(InvalidArchiveNameError):
        archive_name(raw)


def test_archive_name_length_limit_counts_utf8_bytes_after_appending_zip() -> None:
    assert archive_name("x" * 251) == "x" * 251 + ".zip"  # exactly 255 bytes
    with pytest.raises(InvalidArchiveNameError):
        archive_name("é" * 126)  # 252 bytes + ".zip" = 256


# --- entry names (§2.4) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.txt", "report.txt"),
        ("dir/sub/report.txt", "report.txt"),
        ("C:\\Users\\me\\report.txt", "report.txt"),
        ("../../etc/passwd", "passwd"),
        ("dir/", "file"),
        ("", "file"),
        (".", "file"),
        ("..", "file"),
        ("  spaced.txt  ", "spaced.txt"),
        ("bad\x00\x1f\x7fname.txt", "badname.txt"),
        ("\x00", "file"),
        (".env", ".env"),
        ("日本語.txt", "日本語.txt"),
    ],
)
def test_sanitize_entry_name(raw: str, expected: str) -> None:
    assert sanitize_entry_name(raw) == expected


def test_sanitize_entry_name_normalises_to_nfc() -> None:
    assert sanitize_entry_name(unicodedata.normalize("NFD", "é.txt")) == "\u00e9.txt"


def test_long_name_keeps_short_extension() -> None:
    name = sanitize_entry_name("a" * 300 + ".txt")
    assert len(name.encode()) == 255
    assert name == "a" * 251 + ".txt"


def test_long_name_with_long_extension_is_truncated_whole() -> None:
    name = sanitize_entry_name("a" * 250 + "." + "b" * 20)
    assert len(name.encode()) == 255
    assert name == ("a" * 250 + "." + "b" * 20)[:255]


def test_truncation_respects_character_boundaries() -> None:
    name = sanitize_entry_name("é" * 200 + ".txt")  # 2 bytes per char
    assert len(name.encode()) <= 255
    assert name.endswith(".txt")
    assert name.startswith("é")
    name.encode("utf-8")  # still valid


def test_extension_of_exactly_16_bytes_is_preserved() -> None:
    extension = "." + "e" * 15
    name = sanitize_entry_name("s" * 300 + extension)
    assert name.endswith(extension)
    assert len(name.encode()) == 255


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.txt", ("report", ".txt")),
        ("archive.tar.gz", ("archive.tar", ".gz")),
        (".env", (".env", "")),
        ("README", ("README", "")),
        (".env.local", (".env", ".local")),
    ],
)
def test_split_extension(name: str, expected: tuple[str, str]) -> None:
    assert split_extension(name) == expected


# --- de-duplication ---------------------------------------------------------------------


def _dedupe(names: list[str]) -> list[str]:
    deduplicator = EntryNameDeduplicator()
    return [deduplicator.unique(name) for name in names]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (
            ["report.txt", "report.txt", "report.txt"],
            ["report.txt", "report (1).txt", "report (2).txt"],
        ),
        (["README", "README"], ["README", "README (1)"]),
        (["archive.tar.gz", "archive.tar.gz"], ["archive.tar.gz", "archive.tar (1).gz"]),
        ([".env", ".env"], [".env", ".env (1)"]),
        (["a.txt", "a (1).txt", "a.txt"], ["a.txt", "a (1).txt", "a (2).txt"]),
        (["Report.TXT", "report.txt"], ["Report.TXT", "report (1).txt"]),
        (["STRASSE", "straße"], ["STRASSE", "straße (1)"]),  # Unicode case folding
        (["a.txt", "b.txt"], ["a.txt", "b.txt"]),
    ],
)
def test_deduplication(names: list[str], expected: list[str]) -> None:
    assert _dedupe(names) == expected


def test_deduplication_of_many_repeats_is_sequential() -> None:
    result = _dedupe(["x.bin"] * 1000)
    assert result[0] == "x.bin"
    assert result[999] == "x (999).bin"
    assert len({name.casefold() for name in result}) == 1000


def test_deduplicated_long_name_stays_within_limit() -> None:
    long_name = sanitize_entry_name("n" * 300 + ".txt")
    first, second = _dedupe([long_name, long_name])
    assert first == long_name
    assert second.endswith(" (1).txt")
    assert len(second.encode()) <= 255


# --- Content-Disposition (§3) -----------------------------------------------------------


def test_content_disposition_matches_contract_example() -> None:
    assert content_disposition("quarterly report.zip") == (
        "attachment; filename=\"quarterly report.zip\"; filename*=UTF-8''quarterly%20report.zip"
    )


def test_content_disposition_non_ascii_and_specials() -> None:
    header = content_disposition('naïve "q" \\ 文件.zip')
    assert 'filename="na_ve _q_ _ __.zip"' in header
    assert "filename*=UTF-8''na%C3%AFve%20%22q%22%20%5C%20%E6%96%87%E4%BB%B6.zip" in header
    header.encode("ascii")  # header must be pure ASCII


def test_content_disposition_keeps_attr_chars_unencoded() -> None:
    assert content_disposition("a!#$&+^`|~-_.zip").endswith("''a!#$&+^`|~-_.zip")
