"""Typed settings loaded from environment variables. Single source of truth."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="local")
    log_level: str = Field(default="INFO")
    org_id: str = Field(default="ORG-DEMO")

    pcc_api_base_url: str = Field(default="https://hackathon.prod.pulsefoundry.ai")

    database_url: str = Field(...)
    database_url_sync: str = Field(...)
    redis_url: str = Field(default="redis://localhost:6379/0")
    prefect_api_url: str = Field(default="http://localhost:4200/api")

    baseten_api_key: str = Field(default="")
    baseten_model_id_small: str = Field(default="")
    baseten_model_id_large: str = Field(default="")

    elevenlabs_api_key: str = Field(default="")
    elevenlabs_voice_id: str = Field(default="")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
