"""Settings from the environment (and an optional .env file)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./traces.db"
    pricing_file: Path | None = Field(
        default=None, description="YAML price table; the bundled example table when unset"
    )
    ingest_token: str | None = Field(
        default=None, description="When set, POST /v1/traces requires 'Authorization: Bearer ...'"
    )
    read_token: str | None = Field(
        default=None, description="When set, the query API requires 'Authorization: Bearer ...'"
    )
    capture_content: bool = Field(
        default=False,
        description="Keep prompt/completion/tool-argument attributes; dropped at ingest when false",
    )
    redact_pii: bool = Field(
        default=True, description="Mask emails, phone numbers and card numbers in kept attributes"
    )
    max_body_bytes: int = Field(default=5_000_000, ge=1024)
    retention_days: int = Field(default=14, ge=1)
    log_level: str = "INFO"

    @field_validator("ingest_token", "read_token", "pricing_file", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _sync_driver(cls, value: str) -> str:
        """Hosting providers hand out ``postgres://``; SQLAlchemy wants an explicit driver."""
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        return value
