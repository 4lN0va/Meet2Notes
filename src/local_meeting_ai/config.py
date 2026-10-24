from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Environment and command-line configurable application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="M2N_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Meet2Notes"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    data_dir: Path | None = None
    models_dir: Path | None = None
    log_level: str = "INFO"
    max_upload_mb: int = Field(default=2048, ge=1, le=51200)
    max_heavy_jobs: int = Field(default=1, ge=1, le=4)
    ffmpeg_path: Path | None = None
    open_browser: bool = True
    testing: bool = False
    pyannote_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("M2N_PYANNOTE_TOKEN", "PYANNOTE_TOKEN"),
        repr=False,
    )
    # CRM integration (verification against the local mock CRM). The client is
    # only built when crm_base_url is set; without it every CRM route answers
    # 503 instead of guessing an endpoint.
    crm_base_url: str | None = None
    crm_agenda_path: str = "/api/crm/meetings/{meeting_code}/agenda"
    crm_transcripts_path: str = "/api/crm/transcripts"
    crm_summary_path: str = "/api/crm/summaries"
    crm_meeting_ended_path: str = "/api/crm/meetings/{meeting_code}/ended"
    crm_health_path: str = "/api/health"
    crm_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    crm_token: str | None = Field(default=None, repr=False)

    @field_validator("crm_base_url")
    @classmethod
    def validate_crm_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            return None
        parsed = urlsplit(cleaned)
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == "https" and parsed.hostname:
            return cleaned
        # Plain HTTP is only allowed for isolated local test environments.
        if parsed.scheme == "http" and parsed.hostname in loopback_hosts:
            return cleaned
        raise ValueError(
            "crm_base_url must use HTTPS, or plain HTTP with a loopback host "
            "(127.0.0.1 / localhost / ::1) for local verification only"
        )

    @field_validator(
        "crm_agenda_path",
        "crm_transcripts_path",
        "crm_summary_path",
        "crm_meeting_ended_path",
        "crm_health_path",
    )
    @classmethod
    def validate_crm_path(cls, value: str) -> str:
        if (
            not value.startswith("/")
            or ".." in value
            or any(character.isspace() for character in value)
        ):
            raise ValueError("CRM endpoint paths must be absolute URL paths")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Host cannot be empty")
        return value.strip()

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unsupported log level")
        return normalized

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
