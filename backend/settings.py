from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSBLAZE_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://opsblaze:opsblaze@localhost:5432/opsblaze"
    local_token: str | None = None
    environment: Literal["development", "production"] = "development"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "opsblaze-incidents-v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
