"""Centralised settings. Values come from env vars (.env via compose `env_file`)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: Literal["development", "production"] = "development"
    data_dir: Path = Field(default=Path("/data"), alias="REELFORGE_DATA_DIR")
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    max_upload_gb: float = 5.0
    default_chunk_mb: int = 8
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Social publishing. Credentials come from the user's own developer
    # apps on each platform — see docs/publishing.md for setup.
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    instagram_app_id: str = Field(default="", alias="INSTAGRAM_APP_ID")
    instagram_app_secret: str = Field(default="", alias="INSTAGRAM_APP_SECRET")
    tiktok_client_key: str = Field(default="", alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str = Field(default="", alias="TIKTOK_CLIENT_SECRET")
    # Public HTTPS base that reaches this API from the internet (cloudflared
    # tunnel). Instagram requires Meta's servers to FETCH the video from a
    # public URL, so IG publishing is disabled until this is set.
    public_media_base: str = Field(default="", alias="REELFORGE_PUBLIC_MEDIA_BASE")
    # Host-visible bases used to build the OAuth redirect + post-connect hop.
    public_api_base: str = Field(
        default="http://localhost:8001", alias="REELFORGE_PUBLIC_API_BASE"
    )
    web_base: str = Field(default="http://localhost:3000", alias="REELFORGE_WEB_BASE")
    # A folder ReelForge watches for footage — point it at an iCloud or
    # Dropbox folder that syncs from a phone and dropping clips there is
    # enough. Empty disables the scanner.
    watch_dir: str = Field(default="", alias="REELFORGE_WATCH_DIR")
    watch_scan_seconds: float = 30.0
    # A file is only taken once it has stopped growing, so a half-synced
    # clip is never probed.
    watch_settle_seconds: float = 20.0
    # Where finished clips are copied when delivery includes "folder" —
    # point it at a synced folder and they appear on the phone.
    delivery_dir: str = Field(default="", alias="REELFORGE_DELIVERY_DIR")
    # Default delivery when an agent doesn't say: comma-separated
    # links | folder | email.
    delivery_default: str = Field(default="links", alias="REELFORGE_DELIVERY_DEFAULT")
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="", alias="SMTP_FROM")
    smtp_to: str = Field(default="", alias="SMTP_TO")
    caption_preview_timeout_s: float = 10.0
    caption_preview_rpm_per_ip: int = 30

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "reelforge.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"


def get_settings() -> "Settings":
    """Construct a fresh Settings. Useful to tests that mutate env between cases."""
    return Settings()


settings = get_settings()
