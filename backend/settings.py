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
    admission_limit_per_minute: int = 30
    max_active_incidents_per_tenant: int = 20
    max_active_incidents_global: int = 100
    stuck_after_seconds: int = 300
    max_probe_calls_per_incident: int = 3
    experience_retention_days: int = 7
    ai_advisory_enabled: bool = False
    ai_advisory_model: str = "gpt-6.2-sol"


@lru_cache
def get_settings() -> Settings:
    return Settings()
