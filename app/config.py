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

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
    # No gate model — Tedi always responds
    CONVERSATION_HISTORY_WINDOW: int = 40

    # ElevenLabs
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = "ZoiZ8fuDWInAcwPXaVeq"
    ELEVENLABS_MODEL_ID: str = "eleven_flash_v2_5"
    ELEVENLABS_API_BASE_URL: str = "https://api.elevenlabs.io/v1"

    # AWS S3
    S3_BUCKET_NAME: str = "tedi-public-artifacts"
    AWS_REGION: str = "us-east-1"

    # SES
    SES_FROM_EMAIL: str = "tedi@agents.bonecho.ai"
    OUTPUT_RECIPIENTS: str = "labeeb@bonecho.ai,deep@bonecho.ai"

    # Session
    SESSION_TIMEOUT_MINUTES: float = 25.0
    SILENCE_TIMEOUT_SECONDS: float = 1.5

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]


settings = Settings()
