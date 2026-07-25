from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _build_redis_url(host, port, user, password, db) -> str:
    if user and password:
        credentials = f"{user}:{password}@"
    elif password:
        credentials = f":{password}@"
    else:
        credentials = ""
    return f"redis://{credentials}{host}:{port}/{db}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://buybring:buybring@db:5432/buybring"

    # Redis (Railway style)
    redishost: Optional[str] = None
    redisport: str = "6379"
    redisuser: Optional[str] = None
    redispassword: Optional[str] = None
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    @field_validator(
        "kommo_default_pipeline_id",
        "kommo_default_status_id",
        "kommo_menu_pipeline_id",
        "kommo_unreviewed_pipeline_id",
        "kommo_unreviewed_status_id",
        mode="before",
    )
    @classmethod
    def empty_optional_int(cls, value):
        if value is None or value == "":
            return None
        return value

    @model_validator(mode="after")
    def resolve_redis_urls(self) -> "Settings":
        if self.redishost:
            db0 = _build_redis_url(
                self.redishost,
                self.redisport,
                self.redisuser,
                self.redispassword,
                0,
            )
            db1 = _build_redis_url(
                self.redishost,
                self.redisport,
                self.redisuser,
                self.redispassword,
                1,
            )
            if self.celery_broker_url == "redis://redis:6379/0":
                self.celery_broker_url = db0
            if self.celery_result_backend == "redis://redis:6379/1":
                self.celery_result_backend = db1
            if self.redis_url == "redis://redis:6379/0":
                self.redis_url = db0
        return self

    # Telegram
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    webhook_base_url: str = "https://your-domain.com"
    allowed_telegram_user_ids: str = ""
    max_audio_file_size_mb: int = 20

    def get_allowed_user_ids(self) -> List[int]:
        if not self.allowed_telegram_user_ids.strip():
            return []
        try:
            return [
                int(uid.strip())
                for uid in self.allowed_telegram_user_ids.split(",")
                if uid.strip()
            ]
        except ValueError:
            return []

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_whisper_model: str = "whisper-1"

    # Storage
    storage_backend: str = "local"
    local_storage_path: str = "/app/storage"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "eu-central-1"
    s3_bucket_name: str = "buybring-audio"

    # Calendar
    # Use "google" for Google Calendar (recommended on Railway) or "icloud" (legacy).
    calendar_provider: str = "google"

    # iCloud Calendar (legacy CalDAV)
    icloud_username: str = ""
    icloud_app_specific_password: str = ""
    icloud_calendar_name: str = "BBS Работа"
    icloud_caldav_url: str = "https://caldav.icloud.com/.well-known/caldav"
    icloud_calendar_url: str = ""

    # Google Calendar (recommended)
    google_calendar_auth_mode: str = "service_account"
    google_calendar_id: str = ""
    google_calendar_name: str = "BBS Работа"
    google_calendar_timezone: str = "Europe/Warsaw"
    google_calendar_default_duration_minutes: int = 30
    google_calendar_default_reminder_minutes: int = 30
    google_calendar_send_updates: str = "none"
    google_service_account_json: str = ""
    google_service_account_json_base64: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_credentials_file: str = "credentials/google_oauth.json"
    google_token_file: str = "credentials/google_token.json"

    # WhatsApp
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_enabled: bool = False

    # Kommo CRM
    kommo_base_url: str = ""
    kommo_access_token: str = ""
    # Optional: omit both values to use the first stage of the main pipeline.
    kommo_default_pipeline_id: Optional[int] = None
    kommo_default_status_id: Optional[int] = None
    # If set, open-deal list/search only shows this pipeline. Defaults to kommo_default_pipeline_id.
    kommo_menu_pipeline_id: Optional[int] = None
    # 20 pages x 250 leads = safety cap of 5000 scanned leads.
    kommo_open_leads_max_pages: int = 20
    kommo_menu_page_size: int = 8
    telegram_state_ttl_minutes: int = 30
    # "direct" starts processing immediately inside the web service.
    # "celery" sends the job to the voice_notes queue and keeps a fallback watchdog.
    audio_processing_mode: str = "direct"
    audio_queue_fallback_seconds: int = 45
    manager_timezone: str = "Europe/Warsaw"
    kommo_default_task_type_id: int = 1
    kommo_unreviewed_pipeline_id: Optional[int] = None
    kommo_unreviewed_status_id: Optional[int] = None
    # Used when status_id is not set explicitly. Default Kommo stage: Incoming leads.
    kommo_unreviewed_status_name: str = "Incoming leads"
    kommo_unreviewed_page_size: int = 8
    # True = Kommo inbox «Неразобранное» via /api/v4/leads/unsorted (Facebook forms etc.)
    kommo_unreviewed_use_unsorted: bool = True
    # When False, hide leads that already have internal name like "110 - Игрушки".
    kommo_unreviewed_hide_numbered: bool = False

    # Google Sheets lead registry (read-only service account)
    google_sheets_spreadsheet_id: str = ""
    google_sheets_worksheet_name: str = ""
    google_sheets_service_account_json: str = ""
    google_sheets_phone_column: str = "O"
    google_sheets_product_column: str = "P"
    google_sheets_lead_number_column: str = "Y"
    google_sheets_email_column: str = ""
    google_sheets_client_name_column: str = ""
    google_sheets_company_column: str = ""
    google_sheets_header_row: int = 1
    google_sheets_cache_ttl_seconds: int = 300

    # Notion workspace integration
    notion_api_token: str = ""
    notion_auto_sync: bool = True
    notion_clients_database_id: str = ""
    notion_leads_database_id: str = ""
    notion_calls_database_id: str = ""
    notion_tasks_database_id: str = ""
    voice_command_mode: bool = True
    morning_digest_enabled: bool = True
    morning_digest_hour: int = 8

    # App
    app_env: str = "development"
    secret_key: str = "change-me"
    log_level: str = "INFO"

    # 1688 catalog PDF generation
    catalog_enabled: bool = True
    catalog_openai_model: str = ""
    brand_name: str = "Babrik Solutions"
    brand_primary_color: str = "#0B1F3A"
    brand_accent_color: str = "#D8A34A"
    brand_text_color: str = "#20242A"
    brand_logo_path: str = "app/catalog/static/logo.png"
    brand_website: str = ""
    brand_email: str = ""
    brand_phone: str = ""
    playwright_headless: bool = True
    playwright_timeout_seconds: int = 45
    playwright_storage_state: str = "storage/browser/1688_storage_state.json"
    catalog_max_concurrent_jobs: int = 2
    catalog_max_images: int = 12
    catalog_max_image_size_mb: int = 10
    catalog_max_total_download_mb: int = 100
    catalog_pdf_retention_hours: int = 24
    catalog_debug_save_page: bool = False
    catalog_rate_limit_seconds: int = 10
    catalog_processing_mode: str = "celery"
    catalog_extension_api_key: str = ""
    catalog_max_products_per_batch: int = 20

    @property
    def catalog_model(self) -> str:
        return self.catalog_openai_model.strip() or self.openai_model

    @property
    def catalog_api_key(self) -> str:
        return self.catalog_extension_api_key.strip() or self.admin_api_key.strip()

    @property
    def catalog_storage_base(self) -> str:
        return f"{self.local_storage_path.rstrip('/')}/catalog"

    @property
    def catalog_playwright_timeout_ms(self) -> int:
        return self.playwright_timeout_seconds * 1000

    @property
    def catalog_max_image_size_bytes(self) -> int:
        return self.catalog_max_image_size_mb * 1024 * 1024

    @property
    def catalog_max_total_download_bytes(self) -> int:
        return self.catalog_max_total_download_mb * 1024 * 1024

    # Aliases for catalog module compatibility
    @property
    def max_images(self) -> int:
        return self.catalog_max_images

    @property
    def max_image_size_bytes(self) -> int:
        return self.catalog_max_image_size_bytes

    @property
    def max_total_download_bytes(self) -> int:
        return self.catalog_max_total_download_bytes

    @property
    def max_image_size_mb(self) -> int:
        return self.catalog_max_image_size_mb

    @property
    def max_total_download_mb(self) -> int:
        return self.catalog_max_total_download_mb

    @property
    def playwright_timeout_ms(self) -> int:
        return self.catalog_playwright_timeout_ms

    @property
    def debug_save_page(self) -> bool:
        return self.catalog_debug_save_page

    @property
    def storage_temporary(self):
        from pathlib import Path
        return Path(self.catalog_storage_base) / "temporary"

    @property
    def storage_output(self):
        from pathlib import Path
        return Path(self.catalog_storage_base) / "output"

    @property
    def storage_browser(self):
        from pathlib import Path
        return Path(self.local_storage_path.rstrip("/")) / "browser"

    # Security / web API
    admin_api_key: str = ""
    cors_allowed_origins: str = ""
    enable_google_oauth_routes: bool = False
    expose_api_docs: bool = False

    def get_cors_origins(self) -> List[str]:
        if not self.cors_allowed_origins.strip():
            return []
        return [
            value.strip()
            for value in self.cors_allowed_origins.split(",")
            if value.strip()
        ]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
