from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
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
    # Background worker that processes received messages and sends replies. "embedded" runs it
    # inside the web process; "off" leaves it to `python -m lucromei.worker`.
    worker_mode: str = "embedded"
    worker_poll_seconds: float = 1.0
    worker_batch_size: int = 10
    job_max_attempts: int = 8
    # Must be longer than the slowest job (the WhatsApp HTTP timeout).
    job_lease_seconds: int = 120


def is_configured_secret(value: str) -> bool:
    return bool(value) and value != PLACEHOLDER_SECRET


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_business_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().business_timezone)
