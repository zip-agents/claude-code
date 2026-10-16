import pytest

from zip_service.archive.multipart import (
    ContentDisposition,
    ContentTypeError,
    decode_ext_value,
    decode_header_bytes,
    multipart_boundary,
    parse_content_disposition,
    parse_header_params,
)


def test_parse_header_params_basic() -> None:
    assert parse_header_params('Form-Data; Name="files"; filename="a.txt"') == (
        "form-data",
        {"name": "files", "filename": "a.txt"},
    )


def test_parse_header_params_quoted_semicolons_and_escapes() -> None:
    _, params = parse_header_params(r'form-data; name="f"; filename="a;b \"c\" d\\e.txt"')
    assert params["filename"] == 'a;b "c" d\\e.txt'


def test_unescaped_windows_path_keeps_backslashes() -> None:
    _, params = parse_header_params('form-data; name="files"; filename="C:\\dir\\a.txt"')
    assert params["filename"] == "C:\\dir\\a.txt"


def test_first_parameter_occurrence_wins() -> None:
    _, params = parse_header_params('form-data; name="a"; name="b"')
    assert params["name"] == "a"


def test_unquoted_values() -> None:
    _, params = parse_header_params("multipart/form-data; boundary=abc123")
    assert params == {"boundary": "abc123"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("UTF-8''%E2%82%AC%20rates.txt", "\u20ac rates.txt"),
        ("utf-8'en'na%C3%AFve.txt", "na\u00efve.txt"),
        ("ISO-8859-1''caf%E9.txt", "caf\u00e9.txt"),
        ("no-quotes-here", "no-quotes-here"),
    ],
)
def test_decode_ext_value(value: str, expected: str) -> None:
    assert decode_ext_value(value) == expected


def test_filename_star_is_preferred() -> None:
    disposition = parse_content_disposition(
        'form-data; name="files"; filename="fallback.txt"; filename*=UTF-8\'\'r%C3%A9el.txt'
    )
    assert disposition == ContentDisposition(name="files", filename="r\u00e9el.txt")


def test_field_part_has_no_filename() -> None:
    assert parse_content_disposition('form-data; name="filename"') == ContentDisposition(
        name="filename", filename=None
    )


def test_empty_filename_is_distinguished_from_missing() -> None:
    assert parse_content_disposition('form-data; name="files"; filename=""').filename == ""


def test_decode_header_bytes_falls_back_to_latin1() -> None:
    assert decode_header_bytes("é".encode()) == "é"
    assert decode_header_bytes(b"caf\xe9") == "caf\u00e9"


def test_multipart_boundary() -> None:
    assert multipart_boundary("Multipart/Form-Data; boundary=XyZ") == b"XyZ"
    assert multipart_boundary('multipart/form-data; boundary="a b"') == b"a b"


@pytest.mark.parametrize(
    ("content_type", "code"),
    [
        (None, "unsupported-media-type"),
        ("", "unsupported-media-type"),
        ("application/json", "unsupported-media-type"),
        ("multipart/mixed; boundary=x", "unsupported-media-type"),
        ("multipart/form-data", "malformed-request"),
        ("multipart/form-data; boundary=", "malformed-request"),
        ('multipart/form-data; boundary=""', "malformed-request"),
    ],
)
def test_multipart_boundary_errors(content_type: str | None, code: str) -> None:
    with pytest.raises(ContentTypeError) as excinfo:
        multipart_boundary(content_type)
    assert excinfo.value.code == code
