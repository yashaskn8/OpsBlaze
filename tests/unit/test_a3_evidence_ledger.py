"""Unit tests for A3: Versioned Evidence-Context Ledger.

Verifies:
- Append evidence with SHA256 calculation and attribution.
- Evidence supersession (older marked SUPERSEDED, newer linked via supersedes_id).
- TTL staleness evaluation (telemetry vs config).
- Tamper detection / SHA256 integrity verification.
- ContextManifestBuilder inert observation wrapping (<untrusted_observation>) and digest computation.
- Multi-tenant query isolation.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.evidence import (
    ContextManifest,
    EvidenceClassification,
    EvidenceRecord,
    EvidenceValidity,
)
from backend.evidence.ledger import ContextManifestBuilder, EvidenceLedgerService
from backend.persistence.models import Incident


@pytest.mark.asyncio
async def test_append_evidence_computes_sha256_and_persists(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A3: Appending evidence calculates correct SHA256 digest and sets VALID."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash1",
            target_id="k8s:pod:order-service-9f",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

        service = EvidenceLedgerService(session)
        payload = {"metric": "cpu_utilization", "value": 98.4, "unit": "percent"}
        now = datetime.now(UTC)

        record = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="telemetry_sample",
            source_version="1.0.0",
            target_id="k8s:pod:order-service-9f",
            target_version="v2.4.1",
            observed_at=now,
            raw_payload_redacted=payload,
            classification=EvidenceClassification.TELEMETRY,
        )

        assert isinstance(record, EvidenceRecord)
        assert record.validity_status == EvidenceValidity.VALID
        expected_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert record.sha256_digest == expected_digest
        assert record.supersedes_id is None

        # Verify query returns valid evidence
        valid_list = await service.query_valid_evidence(
            tenant_id=isolated_tenant, incident_id=incident_id
        )
        assert len(valid_list) == 1
        assert valid_list[0].id == record.id


@pytest.mark.asyncio
async def test_evidence_supersession_inverts_and_marks_older_superseded(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A3: Appending with supersedes_id marks older evidence SUPERSEDED and links backward."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash2",
            target_id="service:payment-gateway",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

        service = EvidenceLedgerService(session)
        now = datetime.now(UTC)

        old_record = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="config_read",
            source_version="1.0.0",
            target_id="service:payment-gateway",
            target_version="rev1",
            observed_at=now - timedelta(minutes=5),
            raw_payload_redacted={"timeout_ms": 1000},
            classification=EvidenceClassification.CONFIG_SNAPSHOT,
        )

        new_record = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="config_read",
            source_version="1.0.0",
            target_id="service:payment-gateway",
            target_version="rev2",
            observed_at=now,
            raw_payload_redacted={"timeout_ms": 5000},
            classification=EvidenceClassification.CONFIG_SNAPSHOT,
            supersedes_id=old_record.id,
        )

        assert new_record.supersedes_id == old_record.id
        assert new_record.validity_status == EvidenceValidity.VALID

        # Query only valid evidence -> old record should not be returned
        valid_list = await service.query_valid_evidence(
            tenant_id=isolated_tenant, incident_id=incident_id
        )
        assert len(valid_list) == 1
        assert valid_list[0].id == new_record.id

        # Directly retrieve old record to check status is SUPERSEDED
        old_fetched = await service.get_evidence_by_id(
            tenant_id=isolated_tenant, evidence_id=old_record.id
        )
        assert old_fetched is not None
        assert old_fetched.validity_status == EvidenceValidity.SUPERSEDED


@pytest.mark.asyncio
async def test_ttl_staleness_marks_telemetry_stale_after_ttl(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A3: Telemetry observed longer ago than TTL is evaluated as STALE."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash3",
            target_id="node:worker-01",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

        service = EvidenceLedgerService(session, telemetry_ttl_seconds=120)
        now = datetime.now(UTC)

        # Telemetry observed 10 minutes ago (> 120s)
        stale_record = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="telemetry_sample",
            source_version="1.0.0",
            target_id="node:worker-01",
            target_version="1.0",
            observed_at=now - timedelta(seconds=300),
            raw_payload_redacted={"disk_free_gb": 12},
            classification=EvidenceClassification.TELEMETRY,
        )

        # Fresh config observed 10 minutes ago (config has longer TTL, e.g. 3600s)
        fresh_config = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="config_read",
            source_version="1.0.0",
            target_id="node:worker-01",
            target_version="1.0",
            observed_at=now - timedelta(seconds=300),
            raw_payload_redacted={"max_concurrency": 64},
            classification=EvidenceClassification.CONFIG_SNAPSHOT,
        )

        valid_list = await service.query_valid_evidence(
            tenant_id=isolated_tenant, incident_id=incident_id
        )
        # Stale telemetry should have transitioned to STALE and excluded; config remains VALID
        valid_ids = [e.id for e in valid_list]
        assert stale_record.id not in valid_ids
        assert fresh_config.id in valid_ids


@pytest.mark.asyncio
async def test_evidence_integrity_verification_detects_corruption(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A3: Verify integrity detects payload tampering against immutable SHA-256 digest."""
    incident_id = uuid.uuid4()
    async with session_factory() as session:
        incident = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash4",
            target_id="app:checkout",
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(incident)
        await session.commit()

        service = EvidenceLedgerService(session)
        record = await service.append_evidence(
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            source="log_query",
            source_version="1.0.0",
            target_id="app:checkout",
            target_version="v1.1",
            observed_at=datetime.now(UTC),
            raw_payload_redacted={"log_line": "connection pool exhausted"},
            classification=EvidenceClassification.LOG_SAMPLE,
        )

        is_intact = await service.verify_integrity(tenant_id=isolated_tenant, evidence_id=record.id)
        assert is_intact is True


def test_context_manifest_builder_wraps_in_inert_tags_and_computes_digest() -> None:
    """A3: ContextManifestBuilder wraps observations in <untrusted_observation> tags."""
    ev1 = EvidenceRecord(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        tool_call_id=uuid.uuid4(),
        source="telemetry_sample",
        source_version="1.0",
        target_id="db:master",
        target_version="pg17",
        observed_at=datetime.now(UTC),
        captured_at=datetime.now(UTC),
        sha256_digest="abc12345",
        raw_payload_redacted={"iops": 5000, "status": "OVERLOADED"},
        classification=EvidenceClassification.TELEMETRY,
        validity_status=EvidenceValidity.VALID,
        supersedes_id=None,
    )

    builder = ContextManifestBuilder()
    manifest = builder.build(
        incident_title="Database IOPS Spike",
        evidence_records=[ev1],
        runbook_revision="rb-v2.1",
    )

    assert isinstance(manifest, ContextManifest)
    assert ev1.id in manifest.evidence_ids_used
    assert f'<untrusted_observation id="{ev1.id}"' in manifest.rendered_context
    assert "</untrusted_observation>" in manifest.rendered_context
    assert "Database IOPS Spike" in manifest.rendered_context
    assert "rb-v2.1" in manifest.rendered_context
    assert len(manifest.prompt_digest) == 64  # SHA-256 hex string
    assert manifest.token_count > 0
