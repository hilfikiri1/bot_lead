from functools import lru_cache
from typing import List, Optional

from pydantic import model_validator
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
    # Use "icloud" for Apple Calendar on Mac/iPhone, or "google".
    calendar_provider: str = "icloud"

    # iCloud Calendar (CalDAV)
    icloud_username: str = ""
    icloud_app_specific_password: str = ""
    icloud_calendar_name: str = "BBS Работа"
    icloud_caldav_url: str = "https://caldav.icloud.com/.well-known/caldav"
    icloud_calendar_url: str = ""

    # Google Calendar (optional legacy provider)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_credentials_file: str = "credentials/google_oauth.json"
    google_token_file: str = "credentials/google_token.json"
    google_calendar_id: str = "primary"

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
