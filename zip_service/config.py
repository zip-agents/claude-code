"""Service configuration, read from environment variables (and `.env` in development)."""

from typing import Literal

from pydantic import Field, PositiveInt, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """All runtime settings. Field names map to upper-case environment variables.

    Real environment variables take precedence over `.env`; `.env` is optional and is
    resolved relative to the working directory (the project root in development).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"
    zip_compression_level: int = Field(default=6, ge=0, le=9)
    shutdown_grace_seconds: int = Field(default=25, ge=0)

    max_files: PositiveInt = 500
    max_concurrent_requests: PositiveInt = 32
    idle_timeout_seconds: PositiveInt = 30
    max_request_duration_seconds: PositiveInt = 900
    output_buffer_max_bytes: PositiveInt = 16 * 1024 * 1024

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class ConfigError(Exception):
    """Raised when the configuration is invalid; the message is safe to print."""


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from exc


def format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for error in exc.errors():
        variable = ".".join(str(part) for part in error["loc"]).upper() or "<settings>"
        lines.append(f"  {variable}: {error['msg']} (got {error.get('input')!r})")
    return "\n".join(lines)
