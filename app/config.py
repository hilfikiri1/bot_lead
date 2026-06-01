from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://buybring:buybring@db:5432/buybring"

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

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
