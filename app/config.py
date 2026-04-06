"""Application configuration — all settings from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -------------------------------------------------------------------------
    # Core
    # -------------------------------------------------------------------------
    ENVIRONMENT: str = "production"
    SERVICE_NAME: str = "tedi-public"
    APP_ENV: str = "production"

    # -------------------------------------------------------------------------
    # AWS
    # -------------------------------------------------------------------------
    AWS_REGION: str = "us-east-1"

    # -------------------------------------------------------------------------
    # Database — RDS PostgreSQL
    # -------------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://tedi_public:changeme@localhost:5432/tedi_public"

    # -------------------------------------------------------------------------
    # S3
    # -------------------------------------------------------------------------
    S3_BUCKET_NAME: str = "tedi-public-artifacts"

    # -------------------------------------------------------------------------
    # AI services
    # -------------------------------------------------------------------------
    ANTHROPIC_API_KEY: str = ""
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = ""
    DEEPGRAM_API_KEY: str = ""

    # -------------------------------------------------------------------------
    # Email (SES)
    # -------------------------------------------------------------------------
    SES_FROM_EMAIL: str = "tedi@bonecho.ai"
    FOLLOWUP_FROM_EMAIL: str = "sifat@bonecho.ai"

    # -------------------------------------------------------------------------
    # Slack notifications
    # -------------------------------------------------------------------------
    SLACK_WEBHOOK_URL: str = ""          # Incoming webhook for #board-room
    SLACK_CHANNEL: str = "#board-room"

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------
    DAILY_SESSION_CAP: int = 30
    SESSION_TIMEOUT_MINUTES: int = 25

    # -------------------------------------------------------------------------
    # Domain
    # -------------------------------------------------------------------------
    SERVICE_URL: str = "https://tedi-public.bonecho.ai"


settings = Settings()
