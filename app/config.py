from __future__ import annotations

from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/catalog_bot"

    # Brand
    brand_name: str = "Babrik Solutions"
    brand_primary_color: str = "#0B1F3A"
    brand_accent_color: str = "#D8A34A"
    brand_text_color: str = "#20242A"
    brand_logo_path: str = "app/catalog/static/logo.png"
    brand_website: str = ""
    brand_email: str = ""
    brand_phone: str = ""

    # Playwright
    playwright_headless: bool = True
    playwright_timeout_seconds: int = 45
    playwright_storage_state: str = "storage/browser/1688_storage_state.json"

    # Limits
    max_concurrent_jobs: int = 2
    max_images: int = 12
    max_gallery_images: int = 8
    max_detail_images: int = 4
    max_image_size_mb: int = 10
    max_total_download_mb: int = 100
    min_image_side: int = 300

    # Storage
    pdf_retention_hours: int = 24
    temp_storage_dir: str = "storage/temporary"
    output_storage_dir: str = "storage/output"

    # Logging
    log_level: str = "INFO"
    debug_save_page: bool = False

    @field_validator("temp_storage_dir", "output_storage_dir", mode="after")
    @classmethod
    def ensure_dirs_exist(cls, v: str) -> str:
        Path(v).mkdir(parents=True, exist_ok=True)
        return v

    @property
    def playwright_timeout_ms(self) -> int:
        return self.playwright_timeout_seconds * 1000

    @property
    def max_image_size_bytes(self) -> int:
        return self.max_image_size_mb * 1024 * 1024

    @property
    def max_total_download_bytes(self) -> int:
        return self.max_total_download_mb * 1024 * 1024

    @property
    def storage_state_path(self) -> Path | None:
        p = Path(self.playwright_storage_state)
        return p if p.exists() else None


settings = Settings()
