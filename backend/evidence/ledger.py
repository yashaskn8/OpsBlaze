"""A3 Versioned Evidence Ledger & Context Manifest Builder."""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.evidence import (
    ContextManifest,
    EvidenceClassification,
    EvidenceRecord,
    EvidenceValidity,
)
from backend.persistence.models import EvidenceLedger


class EvidenceLedgerService:
    """Service managing versioned, immutable, attributable evidence in PostgreSQL."""

    def __init__(
        self,
        session_or_factory: AsyncSession | async_sessionmaker[AsyncSession],
        telemetry_ttl_seconds: int = 300,
        config_ttl_seconds: int = 86400,
    ) -> None:
        self._session_or_factory = session_or_factory
        self.telemetry_ttl_seconds = telemetry_ttl_seconds
        self.config_ttl_seconds = config_ttl_seconds

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        if isinstance(self._session_or_factory, AsyncSession):
            yield self._session_or_factory
        else:
            async with self._session_or_factory() as sess:
                yield sess

    async def append_evidence(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        source: str,
        source_version: str,
        target_id: str,
        target_version: str,
        observed_at: datetime,
        raw_payload_redacted: dict[str, Any],
        classification: EvidenceClassification,
        tool_call_id: uuid.UUID | None = None,
        supersedes_id: uuid.UUID | None = None,
    ) -> EvidenceRecord:
        """Record an immutable evidence observation and link supersessions."""
        # Calculate SHA256 over deterministic JSON
        payload_bytes = json.dumps(raw_payload_redacted, sort_keys=True).encode("utf-8")
        sha256_digest = hashlib.sha256(payload_bytes).hexdigest()

        async with self._session() as session:
            # Handle backward-pointing supersession
            if supersedes_id is not None:
                stmt = select(EvidenceLedger).where(
                    EvidenceLedger.tenant_id == tenant_id,
                    EvidenceLedger.id == supersedes_id,
                )
                res = await session.execute(stmt)
                older = res.scalar_one_or_none()
                if older is not None:
                    older.validity_status = EvidenceValidity.SUPERSEDED.value

            model = EvidenceLedger(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                incident_id=incident_id,
                tool_call_id=tool_call_id,
                source=source,
                source_version=source_version,
                target_id=target_id,
                target_version=target_version,
                observed_at=observed_at,
                captured_at=datetime.now(UTC),
                sha256_digest=sha256_digest,
                raw_payload_redacted=raw_payload_redacted,
                classification=classification.value,
                validity_status=EvidenceValidity.VALID.value,
                supersedes_id=supersedes_id,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)

            return EvidenceRecord.model_validate(model)

    async def query_valid_evidence(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_id: str | None = None,
        classification: EvidenceClassification | None = None,
    ) -> list[EvidenceRecord]:
        """Query valid evidence, evaluating TTL staleness."""
        async with self._session() as session:
            query = select(EvidenceLedger).where(
                EvidenceLedger.tenant_id == tenant_id,
                EvidenceLedger.incident_id == incident_id,
                EvidenceLedger.validity_status == EvidenceValidity.VALID.value,
            )
            if target_id is not None:
                query = query.where(EvidenceLedger.target_id == target_id)
            if classification is not None:
                query = query.where(EvidenceLedger.classification == classification.value)

            res = await session.execute(query)
            records = list(res.scalars().all())

            now = datetime.now(UTC)
            valid_records: list[EvidenceRecord] = []
            stale_detected = False

            for rec in records:
                # Check TTL
                is_stale = False
                obs_at = rec.observed_at
                if obs_at.tzinfo is None:
                    obs_at = obs_at.replace(tzinfo=UTC)

                if (
                    rec.classification == EvidenceClassification.TELEMETRY.value
                    and (now - obs_at) > timedelta(seconds=self.telemetry_ttl_seconds)
                ) or (
                    rec.classification == EvidenceClassification.CONFIG_SNAPSHOT.value
                    and (now - obs_at) > timedelta(seconds=self.config_ttl_seconds)
                ):
                    is_stale = True

                if is_stale:
                    rec.validity_status = EvidenceValidity.STALE.value
                    stale_detected = True
                else:
                    valid_records.append(EvidenceRecord.model_validate(rec))

            if stale_detected:
                await session.commit()

            return valid_records

    async def get_evidence_by_id(
        self, tenant_id: uuid.UUID, evidence_id: uuid.UUID
    ) -> EvidenceRecord | None:
        """Fetch a single evidence record by ID under tenant isolation."""
        async with self._session() as session:
            stmt = select(EvidenceLedger).where(
                EvidenceLedger.tenant_id == tenant_id,
                EvidenceLedger.id == evidence_id,
            )
            res = await session.execute(stmt)
            rec = res.scalar_one_or_none()
            if rec is None:
                return None
            return EvidenceRecord.model_validate(rec)

    async def verify_integrity(self, tenant_id: uuid.UUID, evidence_id: uuid.UUID) -> bool:
        """Verify immutable payload SHA-256 integrity against tampering."""
        async with self._session() as session:
            stmt = select(EvidenceLedger).where(
                EvidenceLedger.tenant_id == tenant_id,
                EvidenceLedger.id == evidence_id,
            )
            res = await session.execute(stmt)
            rec = res.scalar_one_or_none()
            if rec is None:
                return False

            payload_bytes = json.dumps(rec.raw_payload_redacted, sort_keys=True).encode("utf-8")
            actual_digest = hashlib.sha256(payload_bytes).hexdigest()

            if actual_digest != rec.sha256_digest:
                rec.validity_status = EvidenceValidity.CORRUPTED.value
                await session.commit()
                return False

            return True


class ContextManifestBuilder:
    """Builds inert, prompt-injection-safe context manifests for model consumption."""

    def __init__(self, max_tokens: int = 4000) -> None:
        self.max_tokens = max_tokens

    def build(
        self,
        incident_title: str,
        evidence_records: list[EvidenceRecord],
        runbook_revision: str | None = None,
    ) -> ContextManifest:
        """Construct prompt context with inert xml delimiters and SHA-256 prompt digest."""
        lines: list[str] = [
            f"# Context Manifest for Incident: {incident_title}",
            f"Runbook Revision: {runbook_revision or 'NONE'}",
            "## Verified Observations Ledger:",
        ]

        evidence_ids: list[uuid.UUID] = []
        for ev in evidence_records:
            evidence_ids.append(ev.id)
            payload_str = json.dumps(ev.raw_payload_redacted, indent=2, sort_keys=True)
            lines.append(
                f'<untrusted_observation id="{ev.id}" '
                f'source="{ev.source}" '
                f'target="{ev.target_id}" '
                f'observed_at="{ev.observed_at.isoformat()}">\n'
                f"{payload_str}\n"
                f"</untrusted_observation>"
            )

        rendered_context = "\n".join(lines)
        token_count = max(1, len(rendered_context) // 4)
        prompt_digest = hashlib.sha256(rendered_context.encode("utf-8")).hexdigest()

        return ContextManifest(
            prompt_digest=prompt_digest,
            evidence_ids_used=evidence_ids,
            token_count=token_count,
            runbook_revision=runbook_revision,
            rendered_context=rendered_context,
        )
