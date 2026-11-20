"""One Temporal client boundary: TLS required for production."""

from datetime import timedelta

from temporalio.client import Client
from backend.settings import Settings


def validate_temporal_transport(settings: Settings) -> None:
    if settings.environment == "production" and not settings.temporal_tls_enabled:
        raise ValueError("production Temporal connection requires TLS")
    if settings.temporal_api_key and not settings.temporal_tls_enabled:
        raise ValueError("Temporal API keys require TLS")


async def connect_temporal(
    settings: Settings, *,
    connect_timeout: timedelta | None = None,
) -> Client:
    validate_temporal_transport(settings)
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        tls=settings.temporal_tls_enabled,
        api_key=settings.temporal_api_key or None,
        connect_timeout=connect_timeout,
    )
