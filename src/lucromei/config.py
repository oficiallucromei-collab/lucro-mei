import os
import re
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LUCROMEI_", env_file=".env", hide_input_in_errors=True
    )

    environment: str = "local"
    database_url: str = Field(default="sqlite:///./lucromei.db", repr=False)
    whatsapp_verify_token: str = Field(default="change-me", repr=False)
    whatsapp_app_secret: str = Field(default="change-me", repr=False)
    whatsapp_access_token: str | None = Field(default=None, repr=False)
    whatsapp_phone_number_id: str | None = None
    whatsapp_graph_api_version: str = "v25.0"


def on_cloud_run() -> bool:
    return "K_SERVICE" in os.environ


def validate_cloud_run_settings(settings: Settings) -> None:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    if settings.environment.strip().lower() != "production":
        raise RuntimeError("Cloud Run requires LUCROMEI_ENVIRONMENT=production.")
    for name in ("whatsapp_verify_token", "whatsapp_app_secret", "whatsapp_access_token"):
        value = getattr(settings, name)
        if not value or not value.strip() or value.strip().lower() == "change-me":
            raise RuntimeError(f"Set LUCROMEI_{name.upper()} to a production secret.")
    if not re.fullmatch(r"[0-9]+", settings.whatsapp_phone_number_id or ""):
        raise RuntimeError("LUCROMEI_WHATSAPP_PHONE_NUMBER_ID must be a numeric Meta ID.")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+", settings.whatsapp_graph_api_version):
        raise RuntimeError("LUCROMEI_WHATSAPP_GRAPH_API_VERSION must be a Graph API version.")
    try:
        url = make_url(settings.database_url)
    except (ArgumentError, ValueError):
        raise RuntimeError("Cloud Run requires a valid PostgreSQL database URL.") from None
    if (
        url.drivername != "postgresql+psycopg"
        or not url.host
        or url.query.get("sslmode") != "verify-full"
    ):
        raise RuntimeError("Cloud Run requires a postgresql+psycopg URL with sslmode=verify-full.")


@lru_cache
def get_settings() -> Settings:
    if on_cloud_run():
        settings = Settings(_env_file=None)
        validate_cloud_run_settings(settings)
        return settings
    return Settings()
