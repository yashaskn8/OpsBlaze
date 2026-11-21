from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSBLAZE_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://opsblaze:opsblaze@localhost:5432/opsblaze"
    local_token: str | None = None
    review_token: str | None = None
    review_epoch: int = 1
    approval_ttl_seconds: int = 900
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    idp_event_audience: str = ""  # Dedicated signed revocation event audience
    environment: Literal["development", "production"] = "development"
    otel_traces_endpoint: str = ""  # Off by default; explicit collector only.
    otel_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "opsblaze-incidents-v1"
    worker_workflow_slots: int = Field(default=8, ge=2, le=64)
    worker_activity_slots: int = Field(default=8, ge=1, le=64)
    temporal_tls_enabled: bool = False
    temporal_api_key: str | None = None
    admission_limit_per_minute: int = 30
    max_active_incidents_per_tenant: int = 20
    max_active_incidents_global: int = 100
    stuck_after_seconds: int = 300
    worker_heartbeat_seconds: int = 10
    worker_stale_seconds: int = 30
    ui_event_retention_days: int = 7
    budget_counter_retention_days: int = 7
    max_probe_calls_per_incident: int = 3
    experience_retention_days: int = 7
    ai_advisory_enabled: bool = False
    ai_advisory_model: str = "gpt-6.2-sol"
    ai_token_limit_per_minute: int = 2048


@lru_cache
def get_settings() -> Settings:
    return Settings()
