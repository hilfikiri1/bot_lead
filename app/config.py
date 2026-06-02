from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from functools import lru_cache
from typing import Optional


def _build_redis_url(
    host: str,
    port: str,
    user: Optional[str],
    password: Optional[str],
    db: int,
) -> str:
    """Construct a Redis URL from individual connection components."""
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

    # Individual Redis connection components (provided by Railway)
    redishost: Optional[str] = None
    redisport: str = "6379"
    redisuser: Optional[str] = None
    redispassword: Optional[str] = None

    # Redis / Celery — resolved in the validator below
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    @model_validator(mode="after")
    def resolve_redis_urls(self) -> "Settings":
        """
        Prefer explicit CELERY_BROKER_URL / CELERY_RESULT_BACKEND env vars.
        Fall back to constructing URLs from REDISHOST/REDISPORT/REDISUSER/REDISPASSWORD
        when those are available (standard Railway Redis variable names).
        """
        if self.redishost:
            constructed_db0 = _build_redis_url(
                self.redishost, self.redisport, self.redisuser, self.redispassword, 0
            )
            constructed_db1 = _build_redis_url(
                self.redishost, self.redisport, self.redisuser, self.redispassword, 1
            )

            # Only override if the field still holds its default value, meaning
            # no explicit CELERY_BROKER_URL / CELERY_RESULT_BACKEND was provided.
            if self.celery_broker_url == "redis://redis:6379/0":
                self.celery_broker_url = constructed_db0
            if self.celery_result_backend == "redis://redis:6379/1":
                self.celery_result_backend = constructed_db1
            if self.redis_url == "redis://redis:6379/0":
                self.redis_url = constructed_db0

        return self

    # Telegram
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    webhook_base_url: str = "https://your-domain.com"

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

    # App
    app_env: str = "development"
    secret_key: str = "change-me"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
