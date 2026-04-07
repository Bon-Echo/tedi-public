from pydantic import field_validator
from pydantic_settings import BaseSettings


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
    CONVERSATION_HISTORY_WINDOW: int = 40

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
    OUTPUT_RECIPIENTS: str = "labeeb@bonecho.ai,deep@bonecho.ai"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://tedi:password@localhost:5432/tedi_public"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20

    # Session management
    DAILY_SESSION_CAP: int = 30
    SESSION_TIMEOUT_SECONDS: int = 1500  # 25 minutes
    SILENCE_TIMEOUT_SECONDS: float = 1.5

    # Rate limiting
    SIGNUP_RATE_LIMIT: str = "5/minute"

    # CORS
    CORS_ORIGINS: str = "https://bonecho.ai,http://localhost:3000"

    @field_validator("ANTHROPIC_API_KEY")
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if not v or len(v) < 20:
            raise ValueError(
                "ANTHROPIC_API_KEY is missing or too short. "
                "Set it in .env or environment variables."
            )
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    @property
    def output_recipients_list(self) -> list[str]:
        return [o.strip() for o in self.OUTPUT_RECIPIENTS.split(",")]


settings = Settings()
