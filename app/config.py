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
        "lead_status_sync_pipeline_id",
        "kommo_internal_lead_number_field_id",
        "telegram_owner_user_id",
        "kommo_poland_pipeline_id",
        "kommo_first_contact_status_id",
        "telegram_approval_chat_id",
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
    # Compatibility with the Railway variable name used in the v4 rollout
    # checklist. Both names are accepted and merged.
    telegram_allowed_user_ids: str = ""
    telegram_owner_user_id: Optional[int] = None
    telegram_bot_username: str = ""
    max_audio_file_size_mb: int = 20

    def get_allowed_user_ids(self) -> List[int]:
        raw = ",".join(
            value
            for value in (
                self.allowed_telegram_user_ids.strip(),
                self.telegram_allowed_user_ids.strip(),
            )
            if value
        )
        if not raw:
            return []
        try:
            return list(dict.fromkeys(
                int(uid.strip())
                for uid in raw.split(",")
                if uid.strip()
            ))
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
    kommo_internal_lead_number_field_id: Optional[int] = None
    kommo_unreviewed_pipeline_id: Optional[int] = None
    kommo_unreviewed_status_id: Optional[int] = None
    # Used when status_id is not set explicitly. Default Kommo stage: Incoming leads.
    kommo_unreviewed_status_name: str = "Incoming leads"
    kommo_unreviewed_page_size: int = 8
    # True = Kommo inbox «Неразобранное» via /api/v4/leads/unsorted (Facebook forms etc.)
    kommo_unreviewed_use_unsorted: bool = True
    # When False, hide leads that already have internal name like "110 - Игрушки".
    kommo_unreviewed_hide_numbered: bool = False

    # Google Sheets marketing lead registry.
    google_sheets_spreadsheet_id: str = ""
    google_sheets_worksheet_name: str = ""
    google_sheets_service_account_json: str = ""
    google_sheets_budget_column: str = "M"
    google_sheets_channel_column: str = "N"
    google_sheets_phone_column: str = "O"
    google_sheets_product_column: str = "P"
    google_sheets_region_column: str = "Q"
    google_sheets_lead_number_column: str = "Y"
    google_sheets_email_column: str = ""
    google_sheets_client_name_column: str = ""
    google_sheets_company_column: str = ""
    google_sheets_status_column: str = "W"
    google_sheets_comment_column: str = "X"
    google_sheets_header_row: int = 1
    google_sheets_cache_ttl_seconds: int = 300
    # Spreadsheet writes stay disabled until the manager explicitly enables them
    # in Railway and confirms a concrete update from Telegram.
    google_sheets_write_enabled: bool = False

    # Google Sheets column holding the Facebook Lead Ads ID, when the sheet
    # captures it. Optional: leave empty if the sheet does not track it, in
    # which case matching falls back to phone/email.
    google_sheets_facebook_lead_id_column: str = ""

    # Facebook lead intake: one-lead-at-a-time processing of "Facebook #..."
    # incoming/unsorted Kommo leads (see LEAD_INTAKE.md).
    # Target pipeline/status for the "first contact" move after a new lead is
    # qualified. Falls back to kommo_unreviewed_pipeline_id / kommo_default_*
    # when unset so existing single-pipeline setups keep working unmodified.
    kommo_poland_pipeline_id: Optional[int] = None
    kommo_first_contact_status_id: Optional[int] = None
    kommo_first_contact_status_name: str = "Первый контакт"
    # Chat that receives the one-lead-at-a-time approval preview. Falls back
    # to the chat that issued the /new_leads command when unset.
    telegram_approval_chat_id: Optional[int] = None
    lead_processing_dry_run: bool = False
    # Falls back to manager_timezone when empty.
    lead_processing_timezone: str = ""
    lead_processing_business_start: str = "09:00"
    lead_processing_business_end: str = "18:00"
    # Bump when the note/task template or AI schema changes, so historical
    # jobs can be told apart from freshly generated ones.
    lead_processing_version: int = 1

    # Periodic Kommo <-> Google Sheets lead registry reconciliation.
    # Marketing status (column W) is deliberately independent from Kommo stages.
    # The scheduler is report-only; it never applies updates automatically.
    lead_status_sync_enabled: bool = False
    lead_status_sync_pipeline_id: Optional[int] = None
    lead_status_sync_interval_minutes: int = 180
    lead_status_sync_initial_delay_seconds: int = 90
    lead_status_sync_notify_only_on_differences: bool = True

    # Notion workspace integration
    notion_api_token: str = ""
    notion_auto_sync: bool = True
    notion_clients_database_id: str = ""
    notion_leads_database_id: str = ""
    notion_calls_database_id: str = ""
    notion_tasks_database_id: str = ""
    voice_command_mode: bool = True
    # Kept for compatibility with the legacy command router still used by
    # voice-note and fallback flows in Agent v3.
    natural_command_router_enabled: bool = True
    morning_digest_enabled: bool = True
    morning_digest_hour: int = 8

    # Unified B&BS AI agent
    agent_enabled: bool = True
    agent_auto_voice_mode: bool = True
    agent_planner_model: str = ""
    agent_writer_model: str = ""
    agent_action_ttl_minutes: int = 30
    agent_digest_max_items: int = 10
    agent_sync_max_leads: int = 50
    agent_memory_compact_every: int = 20
    agent_memory_recent_messages: int = 12
    agent_default_client_language: str = "pl"
    agent_invite_ttl_hours: int = 48

    # Google Drive (Agent v4)
    google_drive_enabled: bool = False
    google_drive_root_folder_id: str = ""
    google_drive_inbox_folder_id: str = ""
    google_drive_projects_folder_id: str = ""
    google_drive_project_template_folder_id: str = ""

    # Scheduled agent digests and kaizen reflection
    agent_morning_digest_enabled: bool = False
    agent_morning_digest_hour: int = 8
    agent_evening_digest_enabled: bool = False
    agent_evening_digest_hour: int = 19
    agent_digest_timezone: str = "Europe/Warsaw"
    agent_evening_reflection_enabled: bool = False
    agent_evening_reflection_hour: int = 19
    agent_evening_reflection_reminder_hours: int = 1
    agent_weekly_review_enabled: bool = False
    agent_weekly_review_weekday: int = 6
    agent_weekly_review_hour: int = 19
    agent_weekly_review_min_daily_entries: int = 2

    # AI usage budgets (0 = no blocking)
    agent_daily_ai_budget_usd: float = 0
    agent_monthly_ai_budget_usd: float = 0
    agent_cost_warning_percent: int = 80

    # Agent v5 — Digital Operations Director
    agent_stale_days_default: int = 7
    agent_source_timeout_seconds: int = 8
    google_drive_internal_folder_id: str = ""
    google_drive_restricted_folder_id: str = ""
    google_drive_external_folder_id: str = ""

    # New operational Notion Data Sources (Notion API 2025-09-03)
    notion_projects_data_source_id: str = ""
    notion_tasks_data_source_id: str = ""
    notion_offers_data_source_id: str = ""
    notion_catalogs_data_source_id: str = ""
    notion_communications_data_source_id: str = ""

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
