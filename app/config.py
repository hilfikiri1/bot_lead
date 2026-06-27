from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from functools import lru_cache
from typing import Optional, List


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
            db0 = _build_redis_url(self.redishost, self.redisport, self.redisuser, self.redispassword, 0)
            db1 = _build_redis_url(self.redishost, self.redisport, self.redisuser, self.redispassword, 1)
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

    # Allowed Telegram user IDs for admin commands (comma-separated)
    # Example: ALLOWED_TELEGRAM_USER_IDS=123456789,987654321
    allowed_telegram_user_ids: str = ""

    def get_allowed_user_ids(self) -> List[int]:
        """Parse comma-separated user IDs into a list of ints."""
        if not self.allowed_telegram_user_ids.strip():
            return []
        try:
            return [int(uid.strip()) for uid in self.allowed_telegram_user_ids.split(",") if uid.strip()]
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

    # Google
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

    # Kommo CRM (Stage 1: read-only connection test)
    # KOMMO_BASE_URL should NOT include /api/v4 — that is added in the service
    kommo_base_url: str = ""        # e.g. https://semichev66.kommo.com
    kommo_access_token: str = ""    # Long-lived token from private integration

    # App
    app_env: str = "development"
    secret_key: str = "change-me"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
