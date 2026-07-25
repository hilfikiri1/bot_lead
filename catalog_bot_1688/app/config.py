"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root is the directory that contains the `app` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central settings object. Values are read from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Telegram ----
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # ---- OpenAI ----
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")

    # ---- Database ----
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@db:5432/catalog_bot",
        alias="DATABASE_URL",
    )

    # ---- Branding ----
    brand_name: str = Field(default="Babrik Solutions", alias="BRAND_NAME")
    brand_primary_color: str = Field(default="#0B1F3A", alias="BRAND_PRIMARY_COLOR")
    brand_accent_color: str = Field(default="#D8A34A", alias="BRAND_ACCENT_COLOR")
    brand_text_color: str = Field(default="#20242A", alias="BRAND_TEXT_COLOR")
    brand_logo_path: str = Field(
        default="app/catalog/static/logo.png", alias="BRAND_LOGO_PATH"
    )
    brand_website: str = Field(default="", alias="BRAND_WEBSITE")
    brand_email: str = Field(default="", alias="BRAND_EMAIL")
    brand_phone: str = Field(default="", alias="BRAND_PHONE")

    # ---- Playwright ----
    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")
    playwright_timeout_seconds: int = Field(default=45, alias="PLAYWRIGHT_TIMEOUT_SECONDS")
    playwright_storage_state: str = Field(
        default="storage/browser/1688_storage_state.json",
        alias="PLAYWRIGHT_STORAGE_STATE",
    )
    playwright_max_scrolls: int = Field(default=12, alias="PLAYWRIGHT_MAX_SCROLLS")

    # ---- Limits ----
    max_concurrent_jobs: int = Field(default=2, alias="MAX_CONCURRENT_JOBS")
    max_images: int = Field(default=12, alias="MAX_IMAGES")
    max_gallery_images: int = Field(default=8, alias="MAX_GALLERY_IMAGES")
    max_detail_images: int = Field(default=4, alias="MAX_DETAIL_IMAGES")
    max_image_size_mb: int = Field(default=10, alias="MAX_IMAGE_SIZE_MB")
    max_total_download_mb: int = Field(default=100, alias="MAX_TOTAL_DOWNLOAD_MB")
    min_image_side_px: int = Field(default=300, alias="MIN_IMAGE_SIDE_PX")
    pdf_retention_hours: int = Field(default=24, alias="PDF_RETENTION_HOURS")
    rate_limit_seconds: int = Field(default=15, alias="RATE_LIMIT_SECONDS")

    # ---- Runtime ----
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug_save_page: bool = Field(default=False, alias="DEBUG_SAVE_PAGE")
    storage_dir: str = Field(default="storage", alias="STORAGE_DIR")
    run_mode: str = Field(default="polling", alias="RUN_MODE")
    health_api_host: str = Field(default="0.0.0.0", alias="HEALTH_API_HOST")
    health_api_port: int = Field(default=8000, alias="HEALTH_API_PORT")

    @field_validator("openai_model")
    @classmethod
    def _non_empty_model(cls, value: str) -> str:
        return value or "gpt-5-mini"

    # ---- Derived helpers ----
    @property
    def admin_ids(self) -> list[int]:
        ids: list[int] = []
        for chunk in self.admin_telegram_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                ids.append(int(chunk))
        return ids

    @property
    def storage_path(self) -> Path:
        path = (PROJECT_ROOT / self.storage_dir).resolve()
        return path

    @property
    def temporary_path(self) -> Path:
        return self.storage_path / "temporary"

    @property
    def output_path(self) -> Path:
        return self.storage_path / "output"

    @property
    def browser_path(self) -> Path:
        return self.storage_path / "browser"

    @property
    def storage_state_path(self) -> Path:
        candidate = Path(self.playwright_storage_state)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate

    @property
    def logo_path(self) -> Path:
        candidate = Path(self.brand_logo_path)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate

    def ensure_directories(self) -> None:
        """Create runtime storage directories if they do not yet exist."""
        for directory in (
            self.storage_path,
            self.temporary_path,
            self.output_path,
            self.browser_path,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
