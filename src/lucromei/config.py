from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUCROMEI_", env_file=".env")

    environment: str = "local"
    database_url: str = Field(default="sqlite:///./lucromei.db", repr=False)
    # Reporting periods (days, months) follow the customer's local calendar, not UTC.
    business_timezone: str = "America/Sao_Paulo"
    whatsapp_verify_token: str = Field(default=PLACEHOLDER_SECRET, repr=False)
    whatsapp_app_secret: str = Field(default=PLACEHOLDER_SECRET, repr=False)
    # The /v1 endpoints have no end-user authentication yet. Until they do, they are an
    # internal tool and require this key; when it is empty they are disabled entirely.
    internal_api_key: str = Field(default="", repr=False)
    # WhatsApp Cloud API credentials, from the Meta app dashboard. Without them replies are only
    # recorded locally (mock sender); in production the app refuses to start.
    whatsapp_access_token: str = Field(default="", repr=False)
    whatsapp_phone_number_id: str = ""
    whatsapp_graph_api_version: str = "v25.0"
    whatsapp_timeout_seconds: float = 10.0
    # Terms of use shown during onboarding. Bump the version whenever the text changes.
    terms_version: str = "2026-11-20"
    terms_url: str = ""
    trial_days: int = 14
    # How long a question such as "despesa ou retirada?" stays open before it expires.
    pending_entry_ttl_hours: int = 24
    # AI fallback for messages the rules cannot read. Off unless an API key is set. The AI only
    # suggests; every suggestion is validated and confirmed by the user before it is recorded.
    ai_api_key: str = Field(default="", repr=False)
    ai_model: str = "claude-haiku-5-5"
    ai_timeout_seconds: float = 15.0
    ai_max_input_chars: int = 300
    ai_daily_calls_per_business: int = 30
    # Prices in millionths of a US dollar per million tokens (claude-haiku-5-5: $0.10 / $0.50).
    ai_input_price_micro_usd_per_mtok: int = 100_000
    ai_output_price_micro_usd_per_mtok: int = 500_000
    # Observability. Logs are JSON with a correlation ID and never contain message text or
    # amounts. Sentry is enabled only when a DSN is set.
    log_level: str = "INFO"
    sentry_dsn: str = Field(default="", repr=False)
    sentry_traces_sample_rate: float = 0.0
    # Privacy (LGPD). Raw message text is kept for this many days, then erased; ledger entries
    # stay while the account exists. Deletion requests wait a grace period before running.
    message_text_retention_days: int = 90
    deletion_grace_days: int = 7
    deletion_confirm_minutes: int = 10
    # HMAC key for the anonymous proof kept after an account is erased (32+ chars in production).
    privacy_hash_key: str = Field(default="", repr=False)
    maintenance_interval_seconds: int = 3600
    # Background worker that processes received messages and sends replies. "embedded" runs it
    # inside the web process; "off" leaves it to `python -m lucromei.worker`.
    worker_mode: str = "embedded"
    worker_poll_seconds: float = 1.0
    worker_batch_size: int = 10
    job_max_attempts: int = 8
    # Must be longer than the slowest job (the WhatsApp HTTP timeout).
    job_lease_seconds: int = 120

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, value: str) -> str:
        return normalize_database_url(value)


def normalize_database_url(url: str) -> str:
    """Use psycopg 3 for plain PostgreSQL URLs, as Neon and most providers hand them out.

    SQLAlchemy reads "postgresql://" as the psycopg2 driver, which is not installed.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def is_configured_secret(value: str) -> bool:
    return bool(value) and value != PLACEHOLDER_SECRET


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_business_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().business_timezone)


def check_production_settings(settings: Settings) -> None:
    """Refuse to start in production with settings that would break or mislead users."""

    if settings.environment.strip().lower() != "production":
        return
    missing = []
    if not is_configured_secret(settings.whatsapp_app_secret):
        missing.append("LUCROMEI_WHATSAPP_APP_SECRET")
    if not is_configured_secret(settings.whatsapp_verify_token):
        missing.append("LUCROMEI_WHATSAPP_VERIFY_TOKEN")
    if len(settings.privacy_hash_key) < 32:
        missing.append("LUCROMEI_PRIVACY_HASH_KEY (32+ caracteres)")
    if not settings.terms_url.startswith("https://"):
        missing.append("LUCROMEI_TERMS_URL (https)")
    if missing:
        raise RuntimeError("Configuração de produção incompleta: " + ", ".join(missing))
