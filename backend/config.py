"""Application configuration and environment settings for OpsBlaze / TITAN-20."""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

COMPROMISED_DEFAULT_SECRETS: frozenset[str] = frozenset(
    {
        "opsblaze-secret-auth-signing-key-2026-production",
        "opsblaze-immutable-bundle-hmac-sha256-signer-key-2026",
        "secret",
        "change-me",
        "password",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str
    postgres_db: str = "opsblaze"

    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_db_file: str = ".local/temporal.db"

    # Stage 11 & 11.1: Server-side Authentication & Policy Signing
    opsblaze_env: str = "development"
    auth_secret_key: str | None = None
    policy_signing_secret: str | None = None
    policy_signing_key_id: str = "opsblaze-signer-v1"

    # Production identity is verified by the configured provider's OAuth2 introspection endpoint.
    identity_issuer: str | None = None
    identity_introspection_url: str | None = None
    identity_client_id: str | None = None
    identity_client_secret: str | None = None
    identity_audience: str | None = None

    @model_validator(mode="after")
    def validate_secrets(self) -> Settings:
        env_norm = self.opsblaze_env.strip().lower()
        if env_norm == "production":
            if not self.auth_secret_key or len(self.auth_secret_key.strip()) < 32:
                raise ValueError(
                    "Production configuration requires non-empty AUTH_SECRET_KEY with at least 32 characters."
                )
            if not self.policy_signing_secret or len(self.policy_signing_secret.strip()) < 32:
                raise ValueError(
                    "Production configuration requires non-empty POLICY_SIGNING_SECRET with at least 32 characters."
                )
            if self.auth_secret_key.strip() in COMPROMISED_DEFAULT_SECRETS:
                raise ValueError(
                    "Insecure compromised default secret key detected for AUTH_SECRET_KEY. Key rotation required."
                )
            if self.policy_signing_secret.strip() in COMPROMISED_DEFAULT_SECRETS:
                raise ValueError(
                    "Insecure compromised default secret key detected for POLICY_SIGNING_SECRET. Key rotation required."
                )
        else:
            # Safe fallbacks strictly for local development and unit tests
            if not self.auth_secret_key:
                self.auth_secret_key = "opsblaze-dev-auth-secret-key-32bytes-hex-safe"
            if not self.policy_signing_secret:
                self.policy_signing_secret = "opsblaze-dev-policy-signing-secret-32bytes-hex-safe"
        return self

    @property
    def database_url(self) -> URL:
        # URL.create escapes special characters in the password (e.g. '@').
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
