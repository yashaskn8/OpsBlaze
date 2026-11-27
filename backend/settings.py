from functools import lru_cache
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.release_profile import COMMUNITY_PROFILE, ReleaseProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSBLAZE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://opsblaze:opsblaze@localhost:5432/opsblaze"
    projector_database_url: str = ""  # Dedicated DB principal; never reuse main DSN.
    local_token: str | None = None
    review_token: str | None = None
    review_epoch: int = 1
    approval_ttl_seconds: int = 900
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    idp_event_audience: str = ""  # Dedicated signed revocation event audience
    environment: Literal["development", "production"] = "development"
    release_profile: ReleaseProfile = COMMUNITY_PROFILE
    effect_adapter: Literal["simulator"] = "simulator"
    live_policy_promotion: bool = False
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
    # Set separately on each authorized worker host; never assume a UUID
    # or hostname string proves physical isolation or actual failover.
    worker_host_id: str = ""
    # Opt-in G3 topology check for dispatcher + activity workers. A green
    # heartbeat still needs independent host inventory and fault evidence.
    min_distinct_worker_hosts: int = Field(default=2, ge=2, le=16)
    ui_event_retention_days: int = 7
    budget_counter_retention_days: int = 7
    max_probe_calls_per_incident: int = 3
    experience_retention_days: int = 7
    ai_advisory_enabled: bool = False
    ai_advisory_model: str = "gpt-6.2-sol"
    ai_token_limit_per_minute: int = 2048

    @field_validator("local_token", "review_token")
    @classmethod
    def reject_sample_token(cls, value: str | None) -> str | None:
        if value in {"replace-with-a-long-random-local-token", "use-a-different-long-random-reviewer-token"}:
            return None
        return value

    @field_validator("live_policy_promotion")
    @classmethod
    def refuse_live_policy_promotion(cls, enabled: bool) -> bool:
        if enabled:
            raise ValueError("live policy promotion is unavailable in Community Edition")
        return enabled


@lru_cache
def get_settings() -> Settings:
    credentials_file = os.environ.get("OPSBLAZE_CREDENTIALS_FILE")
    if not credentials_file:
        return Settings()
    path = Path(credentials_file)
    if not path.is_file():
        raise ValueError("OPSBLAZE_CREDENTIALS_FILE must name an existing local file")
    return Settings(_env_file=(".env", path))
