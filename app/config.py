from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/catalog_bot"

    brand_name: str = "Babrik Solutions"
    brand_primary_color: str = "#0B1F3A"
    brand_accent_color: str = "#D8A34A"
    brand_text_color: str = "#20242A"
    brand_logo_path: Path = Path("app/catalog/static/logo.png")
    brand_website: str = ""
    brand_email: str = ""
    brand_phone: str = ""

    playwright_headless: bool = True
    playwright_timeout_seconds: int = 45
    playwright_storage_state: Path = Path("storage/browser/1688_storage_state.json")

    max_concurrent_jobs: int = Field(default=2, ge=1, le=10)
    max_images: int = Field(default=12, ge=1, le=24)
    max_gallery_images: int = Field(default=8, ge=1, le=12)
    max_detail_images: int = Field(default=4, ge=0, le=12)
    max_image_size_mb: int = Field(default=10, ge=1, le=50)
    max_total_download_mb: int = Field(default=100, ge=5, le=500)
    pdf_retention_hours: int = Field(default=24, ge=1)
    log_level: str = "INFO"
    debug_save_page: bool = False
    request_rate_limit_seconds: int = Field(default=10, ge=0)

    storage_root: Path = Path("storage")
    temporary_dir: Path = Path("storage/temporary")
    output_dir: Path = Path("storage/output")

    @field_validator("brand_primary_color", "brand_accent_color", "brand_text_color")
    @classmethod
    def validate_color(cls, value: str) -> str:
        if not value.startswith("#") or len(value) not in {4, 7}:
            raise ValueError("brand colors must be hex values")
        return value

    def ensure_directories(self) -> None:
        for path in (self.temporary_dir, self.output_dir, self.playwright_storage_state.parent):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
