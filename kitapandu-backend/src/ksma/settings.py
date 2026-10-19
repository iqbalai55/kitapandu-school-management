"""KSMA settings — pydantic-settings 2.x.

Per ADR0001, all env vars are prefixed `KITAPANDU_`. See `.env.example` for the list.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="KITAPANDU_",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = "development"
    log_level: str = "INFO"

    # --- Postgres ---
    postgres_dsn: PostgresDsn
    postgres_dsn_sync: str | None = None  # used by Alembic

    # --- JWT ---
    jwt_secret: SecretStr
    jwt_issuer: str = "kitapandu-api"
    jwt_audience: str = "kitapandu-web"
    access_token_ttl_seconds: int = 900  # 15 min
    refresh_token_ttl_seconds: int = 7 * 24 * 3600  # 7 days

    # --- MinIO / S3 ---
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str
    minio_secret_key: SecretStr
    minio_secure: bool = False
    s3_bucket: str = "ksma-attachments"
    presigned_url_ttl_seconds: int = 300  # 5 min

    # --- Outbox worker ---
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 50

    # --- File upload (NFR07) ---
    max_file_bytes: int = 10 * 1024 * 1024  # 10 MB initial proposal

    # --- Time (per docs: Asia/Jakarta proposed for pilot) ---
    default_timezone: str = "Asia/Jakarta"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]