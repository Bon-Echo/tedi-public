from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Sentinel default for the admin session secret. Local/dev keeps working with
# this value, but `Settings` raises at boot if APP_ENV=production still has it.
_ADMIN_SESSION_SECRET_DEV_DEFAULT = "dev-only-change-me-in-production-32+chars"


class Settings(BaseSettings):
    model_config = {"case_sensitive": True, "env_file": ".env", "extra": "ignore"}

    # Application
    APP_NAME: str = "tedi-public"
    APP_ENV: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "WARNING"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    PUBLIC_BASE_URL: str = "https://tedi-public.bonecho.ai"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
    ANTHROPIC_GATE_MODEL: str = "claude-haiku-4-5-20251001"
    CONVERSATION_HISTORY_WINDOW: int = 30

    # ElevenLabs
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = "ZoiZ8fuDWInAcwPXaVeq"
    ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"
    ELEVENLABS_API_BASE_URL: str = "https://api.elevenlabs.io/v1"

    # AWS
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "tedi-artifacts"

    # SES
    SES_FROM_EMAIL: str = "tedi@bonecho.ai"
    SES_REGION: str = "us-east-1"
    OUTPUT_RECIPIENTS: str = "labeeb@bonecho.ai,deep@bonecho.ai,sifat@bonecho.ai"
    FOLLOWUP_FROM_EMAIL: str = "sifat@bonecho.ai"

    # Slack
    SLACK_WEBHOOK_URL: str = ""
    SLACK_CHANNEL: str = "#board-room"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://tedi:password@localhost:5432/tedi_public"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # Session management
    DAILY_SESSION_CAP: int = 30
    SESSION_TIMEOUT_SECONDS: int = 720  # 12 minutes hard cap
    SILENCE_TIMEOUT_SECONDS: float = 1.5

    # Booking
    BOOKING_URL: str = "https://bonecho.ai/book"

    # Rate limiting
    SIGNUP_RATE_LIMIT: str = "5/minute"

    # CORS (public Tedi origins)
    CORS_ORIGINS: str = "https://bonecho.ai,http://localhost:3000"

    # Admin / SSO
    ADMIN_ALLOWED_DOMAIN: str = "bonecho.ai"
    ADMIN_SESSION_SECRET: str = _ADMIN_SESSION_SECRET_DEV_DEFAULT
    ADMIN_SESSION_TTL_SECONDS: int = 60 * 60 * 12  # 12h
    ADMIN_UI_ORIGIN: str = "http://localhost:3001"
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    @field_validator("ANTHROPIC_API_KEY")
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if not v or len(v) < 20:
            raise ValueError(
                "ANTHROPIC_API_KEY is missing or too short. "
                "Set it in .env or environment variables."
            )
        return v

    @model_validator(mode="after")
    def _enforce_admin_secret_in_production(self) -> "Settings":
        """Refuse to boot in production with the placeholder admin secret.

        Local/dev keeps the convenience default so signed-cookie tests work
        without setup. Anything signed with the placeholder is trivially
        forgeable — production must set its own random value.
        """
        if self.APP_ENV == "production":
            secret = (self.ADMIN_SESSION_SECRET or "").strip()
            if (
                not secret
                or secret == _ADMIN_SESSION_SECRET_DEV_DEFAULT
                or len(secret) < 32
            ):
                raise ValueError(
                    "ADMIN_SESSION_SECRET must be set to a random value of "
                    "at least 32 characters when APP_ENV=production."
                )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def output_recipients_list(self) -> list[str]:
        return [o.strip() for o in self.OUTPUT_RECIPIENTS.split(",") if o.strip()]

    @property
    def admin_cors_origins_list(self) -> list[str]:
        """Admin/auth surface CORS — strictly the dashboard origin."""
        return [o.strip() for o in self.ADMIN_UI_ORIGIN.split(",") if o.strip()]


settings = Settings()
