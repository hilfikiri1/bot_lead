"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/catalog_bot",
        alias="DATABASE_URL",
    )

    brand_name: str = Field(default="Babrik Solutions", alias="BRAND_NAME")
    brand_primary_color: str = Field(default="#0B1F3A", alias="BRAND_PRIMARY_COLOR")
    brand_accent_color: str = Field(default="#D8A34A", alias="BRAND_ACCENT_COLOR")
    brand_text_color: str = Field(default="#20242A", alias="BRAND_TEXT_COLOR")
    brand_logo_path: str = Field(
        default="app/catalog/static/logo.png",
        alias="BRAND_LOGO_PATH",
    )
    brand_website: str = Field(default="", alias="BRAND_WEBSITE")
    brand_email: str = Field(default="", alias="BRAND_EMAIL")
    brand_phone: str = Field(default="", alias="BRAND_PHONE")

    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")
    playwright_timeout_seconds: int = Field(default=45, alias="PLAYWRIGHT_TIMEOUT_SECONDS")
    playwright_storage_state: str = Field(
        default="storage/browser/1688_storage_state.json",
        alias="PLAYWRIGHT_STORAGE_STATE",
    )

    max_concurrent_jobs: int = Field(default=2, alias="MAX_CONCURRENT_JOBS")
    max_images: int = Field(default=12, alias="MAX_IMAGES")
    max_image_size_mb: int = Field(default=10, alias="MAX_IMAGE_SIZE_MB")
    max_total_download_mb: int = Field(default=100, alias="MAX_TOTAL_DOWNLOAD_MB")
    pdf_retention_hours: int = Field(default=24, alias="PDF_RETENTION_HOURS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug_save_page: bool = Field(default=False, alias="DEBUG_SAVE_PAGE")

    bot_mode: str = Field(default="polling", alias="BOT_MODE")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    rate_limit_seconds: int = Field(default=10, alias="RATE_LIMIT_SECONDS")

    storage_temporary: Path = Path("storage/temporary")
    storage_output: Path = Path("storage/output")
    storage_browser: Path = Path("storage/browser")

    @property
    def max_image_size_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    @property
    def max_total_download_bytes(self) -> int:
        return self.max_total_download_mb * 1024 * 1024

    @property
    def playwright_timeout_ms(self) -> int:
        return self.playwright_timeout_seconds * 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
