import os
import re
from datetime import date
from functools import lru_cache
from typing import Literal

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
    growth_enabled: bool = False
    growth_enforce_trial: bool = False
    growth_public_requests_per_minute: int = Field(default=240, ge=1, le=100_000)
    growth_whatsapp_number: str = Field(default="", pattern=r"^[0-9]{0,15}$")
    growth_public_url: str = Field(default="", pattern=r"^(|https://[A-Za-z0-9.-]+(?::[0-9]+)?)$")
    growth_privacy_url: str = Field(default="", pattern=r"^(|https://[^\s]+)$")
    growth_terms_url: str = Field(default="", pattern=r"^(|https://[^\s]+)$")
    growth_founder_token: str = Field(default="", min_length=0, repr=False)
    growth_trial_hold_key: str = Field(default="", repr=False)
    growth_trial_hold_days: int = Field(default=180, ge=1, le=365)
    growth_trial_days: int = Field(default=14, ge=1, le=365)
    growth_activation_count: int = Field(default=3, ge=1, le=100)
    growth_activation_hours: int = Field(default=48, ge=1, le=8760)
    growth_price_cents: int = Field(default=2490, ge=1, le=50_000_000)
    growth_budget_cents: int = Field(default=15000, gt=0)
    growth_budget_alert_percent: int = Field(default=80, ge=1, le=100)
    growth_experiment_start: date | None = None
    growth_campaign_days: int = Field(default=10, gt=0)
    growth_target_activated: int = Field(default=5, gt=0)
    growth_target_paid: int = Field(default=2, gt=0)
    growth_variable_cost_cents: int | None = Field(default=None, ge=0)
    growth_referrals_enabled: bool = False
    growth_referral_reward_days: int = Field(default=7, ge=1, le=30)
    growth_report_language: Literal["pt", "en"] = "pt"
    growth_ai_key: str = Field(default="", repr=False)
    growth_ai_model: str = ""
    growth_lifecycle_enabled: bool = False
    growth_lifecycle_templates: dict[str, str] = Field(default_factory=dict)
    growth_stripe_key: str = Field(default="", repr=False)
    growth_stripe_price: str = ""
    growth_stripe_webhook_secret: str = Field(default="", repr=False)
    growth_stripe_live: bool = False


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
