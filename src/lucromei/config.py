from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LUCROMEI_", env_file=".env")

    environment: str = "local"
    database_url: str = "sqlite:///./lucromei.db"
    whatsapp_verify_token: str = "change-me"
    whatsapp_app_secret: str = "change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()
