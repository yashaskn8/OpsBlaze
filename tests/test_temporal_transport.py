"""Production transport configuration cannot permit plaintext credentials."""

import pytest
from backend.settings import Settings
from backend.temporal_connection import validate_temporal_transport


def test_production_rejects_plaintext_temporal():
    with pytest.raises(ValueError, match="requires TLS"):
        validate_temporal_transport(Settings(environment="production"))
    validate_temporal_transport(Settings(
        environment="production", temporal_tls_enabled=True,
    ))


def test_temporal_api_key_never_sent_without_tls():
    with pytest.raises(ValueError, match="require TLS"):
        validate_temporal_transport(Settings(temporal_api_key="fixture-only"))
    validate_temporal_transport(Settings(
        temporal_api_key="fixture-only", temporal_tls_enabled=True,
    ))
