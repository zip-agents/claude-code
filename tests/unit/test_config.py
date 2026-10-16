from pathlib import Path

import pytest

from zip_service.config import ConfigError, Settings, load_settings

ALL_VARIABLES = [
    "PORT",
    "LOG_LEVEL",
    "ZIP_COMPRESSION_LEVEL",
    "SHUTDOWN_GRACE_SECONDS",
    "MAX_FILES",
    "MAX_CONCURRENT_REQUESTS",
    "IDLE_TIMEOUT_SECONDS",
    "MAX_REQUEST_DURATION_SECONDS",
    "OUTPUT_BUFFER_MAX_BYTES",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ALL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # Run from an empty directory so a developer's .env cannot leak into the tests.
    monkeypatch.chdir(tmp_path)


def test_defaults_without_env_or_dotenv() -> None:
    settings = load_settings()
    assert settings == Settings(
        port=8000,
        log_level="INFO",
        zip_compression_level=6,
        shutdown_grace_seconds=25,
        max_files=500,
        max_concurrent_requests=32,
        idle_timeout_seconds=30,
        max_request_duration_seconds=900,
        output_buffer_max_bytes=16777216,
    )


def test_env_example_lists_every_variable_with_its_default() -> None:
    example = Path(__file__).parents[2] / ".env.example"
    values = dict(
        line.split("=", 1)
        for line in example.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert sorted(values) == sorted(ALL_VARIABLES)
    defaults = Settings()
    for name, value in values.items():
        assert str(getattr(defaults, name.lower())) == value


def test_dotenv_is_loaded(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PORT=9000\nMAX_FILES=3\n")
    settings = load_settings()
    assert settings.port == 9000
    assert settings.max_files == 3


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("PORT=9000\n")
    monkeypatch.setenv("PORT", "9100")
    assert load_settings().port == 9100


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert load_settings().log_level == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "70000"),
        ("PORT", "abc"),
        ("LOG_LEVEL", "TRACE"),
        ("ZIP_COMPRESSION_LEVEL", "10"),
        ("ZIP_COMPRESSION_LEVEL", "-1"),
        ("SHUTDOWN_GRACE_SECONDS", "-1"),
        ("MAX_FILES", "0"),
        ("MAX_CONCURRENT_REQUESTS", "-3"),
        ("IDLE_TIMEOUT_SECONDS", "0"),
        ("MAX_REQUEST_DURATION_SECONDS", "1.5"),
        ("OUTPUT_BUFFER_MAX_BYTES", "lots"),
    ],
)
def test_invalid_values_are_rejected_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert message.startswith("Invalid configuration:")
    assert name in message


def test_compression_level_zero_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIP_COMPRESSION_LEVEL", "0")
    assert load_settings().zip_compression_level == 0
