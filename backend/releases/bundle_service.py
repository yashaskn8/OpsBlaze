"""Feature E6 — Immutable Signed Policy Bundle Service.

Responsible for:
- Creating draft policy bundles from E5 candidate reports or baseline bootstrapping.
- Canonicalizing bundle payloads according to RFC 8785.
- Computing SHA-256 digests and cryptographic HMAC-SHA256 signatures with domain separation.
- Sealing bundles into immutable artifacts.
- Verifying cryptographic signature and content integrity.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.domain.release import (
    BundleDigestMismatchError,
    BundleNotFoundError,
    BundleNotSealedError,
    BundleSignatureInvalidError,
    BundleStatus,
    canonicalize_bundle_payload,
    compute_bundle_digest,
    compute_bundle_signature,
    verify_bundle_signature,
)
from backend.persistence.models import (
    CandidateEvaluationManifest,
    CandidateEvaluationReport,
    PolicyBundle,
)

logger = logging.getLogger("opsblaze.releases.bundle_service")


class PolicyBundleService:
    """Manages policy bundle creation, sealing, integrity verification, and retrieval."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_bundle(
        self,
        *,
        tenant_id: uuid.UUID,
        bundle_name: str,
        base_policy_version: str,
        target_component: str,
        created_by: str,
        model_id: str,
        model_version: str,
        tool_catalog_version: str,
        a2_grant_version: str,
        source_provenance_digest: str,
        proposal_diff: dict[str, Any] | None = None,
        prompt_templates: dict[str, Any] | None = None,
        active_lesson_ids: list[str] | None = None,
        source_manifest_id: uuid.UUID | None = None,
        source_report_id: uuid.UUID | None = None,
    ) -> PolicyBundle:
        """Create a new policy bundle in DRAFT status."""
        async with self._session_factory() as session:
            # If source report provided, verify it exists and is promotion eligible
            if source_report_id is not None:
                report = await session.get(CandidateEvaluationReport, source_report_id)
                if report is None or report.tenant_id != tenant_id:
                    raise ValueError(f"Source candidate evaluation report {source_report_id} not found.")
                if report.report_status != "PASS" or not report.promotion_eligible:
                    raise ValueError(
                        f"Cannot create bundle: source report status is '{report.report_status}' and promotion_eligible={report.promotion_eligible}."
                    )
                if source_manifest_id is None:
                    source_manifest_id = report.manifest_id

            if source_manifest_id is not None:
                manifest = await session.get(CandidateEvaluationManifest, source_manifest_id)
                if manifest is None or manifest.tenant_id != tenant_id:
                    raise ValueError(f"Source candidate evaluation manifest {source_manifest_id} not found.")

            bundle = PolicyBundle(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                version=1,
                bundle_name=bundle_name,
                bundle_sha256="0" * 64,  # temporary placeholder until sealed
                signature="0" * 128,      # temporary placeholder until sealed
                signature_key_id="unsealed",
                base_policy_version=base_policy_version,
                target_component=target_component,
                proposal_diff=proposal_diff,
                prompt_templates=prompt_templates,
                active_lesson_ids=active_lesson_ids,
                model_id=model_id,
                model_version=model_version,
                tool_catalog_version=tool_catalog_version,
                a2_grant_version=a2_grant_version,
                source_manifest_id=source_manifest_id,
                source_report_id=source_report_id,
                source_provenance_digest=source_provenance_digest,
                status=BundleStatus.DRAFT.value,
                created_by=created_by,
                created_at=datetime.now(UTC),
                sealed_at=None,
            )
            session.add(bundle)
            await session.commit()
            await session.refresh(bundle)
            return bundle

    async def seal_bundle(
        self,
        *,
        tenant_id: uuid.UUID,
        bundle_id: uuid.UUID,
    ) -> PolicyBundle:
        """Canonicalizes, digests, and signs a bundle, locking it into SEALED status."""
        settings = get_settings()
        secret = settings.policy_signing_secret or "opsblaze-dev-policy-signing-secret-hex-safe"
        key_id = settings.policy_signing_key_id or "key-1"

        async with self._session_factory() as session:
            bundle = await session.get(PolicyBundle, bundle_id)
            if bundle is None or bundle.tenant_id != tenant_id:
                raise BundleNotFoundError(bundle_id)

            if bundle.status == BundleStatus.SEALED.value:
                # Idempotent return if already sealed and valid
                await self.verify_bundle_integrity(tenant_id=tenant_id, bundle=bundle)
                return bundle

            canonical_dict = {
                "tenant_id": str(bundle.tenant_id),
                "bundle_name": bundle.bundle_name,
                "base_policy_version": bundle.base_policy_version,
                "target_component": bundle.target_component,
                "proposal_diff": bundle.proposal_diff or {},
                "prompt_templates": bundle.prompt_templates or {},
                "active_lesson_ids": bundle.active_lesson_ids or [],
                "model_id": bundle.model_id,
                "model_version": bundle.model_version,
                "tool_catalog_version": bundle.tool_catalog_version,
                "a2_grant_version": bundle.a2_grant_version,
                "source_manifest_id": str(bundle.source_manifest_id) if bundle.source_manifest_id else None,
                "source_report_id": str(bundle.source_report_id) if bundle.source_report_id else None,
                "source_provenance_digest": bundle.source_provenance_digest,
            }

            canonical_bytes = canonicalize_bundle_payload(canonical_dict)
            digest = compute_bundle_digest(canonical_bytes)
            sig = compute_bundle_signature(digest, secret, key_id)

            bundle.bundle_sha256 = digest
            bundle.signature = sig
            bundle.signature_key_id = key_id
            bundle.status = BundleStatus.SEALED.value
            bundle.sealed_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(bundle)
            logger.info("Policy bundle %s sealed successfully (digest=%s).", bundle.id, digest)
            return bundle

    async def verify_bundle_integrity(
        self,
        *,
        tenant_id: uuid.UUID,
        bundle: PolicyBundle,
    ) -> bool:
        """Verifies canonical payload digest and cryptographic HMAC-SHA256 signature."""
        if bundle.status != BundleStatus.SEALED.value:
            raise BundleNotSealedError(bundle.id, bundle.status)

        canonical_dict = {
            "tenant_id": str(bundle.tenant_id),
            "bundle_name": bundle.bundle_name,
            "base_policy_version": bundle.base_policy_version,
            "target_component": bundle.target_component,
            "proposal_diff": bundle.proposal_diff or {},
            "prompt_templates": bundle.prompt_templates or {},
            "active_lesson_ids": bundle.active_lesson_ids or [],
            "model_id": bundle.model_id,
            "model_version": bundle.model_version,
            "tool_catalog_version": bundle.tool_catalog_version,
            "a2_grant_version": bundle.a2_grant_version,
            "source_manifest_id": str(bundle.source_manifest_id) if bundle.source_manifest_id else None,
            "source_report_id": str(bundle.source_report_id) if bundle.source_report_id else None,
            "source_provenance_digest": bundle.source_provenance_digest,
        }

        canonical_bytes = canonicalize_bundle_payload(canonical_dict)
        computed_digest = compute_bundle_digest(canonical_bytes)
        if computed_digest != bundle.bundle_sha256:
            raise BundleDigestMismatchError(computed=computed_digest, declared=bundle.bundle_sha256)

        settings = get_settings()
        secret = settings.policy_signing_secret or "opsblaze-dev-policy-signing-secret-hex-safe"
        if not verify_bundle_signature(bundle.bundle_sha256, bundle.signature, secret, bundle.signature_key_id):
            raise BundleSignatureInvalidError(bundle.id)

        return True

    async def get_bundle(self, *, tenant_id: uuid.UUID, bundle_id: uuid.UUID) -> PolicyBundle:
        async with self._session_factory() as session:
            bundle = await session.get(PolicyBundle, bundle_id)
            if bundle is None or bundle.tenant_id != tenant_id:
                raise BundleNotFoundError(bundle_id)
            return bundle

    async def list_bundles(
        self,
        *,
        tenant_id: uuid.UUID,
        target_component: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PolicyBundle]:
        async with self._session_factory() as session:
            stmt = select(PolicyBundle).where(PolicyBundle.tenant_id == tenant_id)
            if target_component:
                stmt = stmt.where(PolicyBundle.target_component == target_component)
            if status:
                stmt = stmt.where(PolicyBundle.status == status)
            stmt = stmt.order_by(PolicyBundle.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def ensure_bootstrap_incumbent(
        self,
        *,
        tenant_id: uuid.UUID,
        target_component: str,
        created_by: str = "system-bootstrap",
    ) -> PolicyBundle:
        """Idempotently ensures a sealed baseline incumbent bundle exists for component."""
        async with self._session_factory() as session:
            stmt = (
                select(PolicyBundle)
                .where(
                    PolicyBundle.tenant_id == tenant_id,
                    PolicyBundle.target_component == target_component,
                    PolicyBundle.status == BundleStatus.SEALED.value,
                    PolicyBundle.source_report_id.is_(None),
                )
                .order_by(PolicyBundle.created_at.asc())
            )
            result = await session.execute(stmt)
            existing = result.scalars().first()
            if existing is not None:
                return existing

        bundle = await self.create_bundle(
            tenant_id=tenant_id,
            bundle_name=f"{target_component}-incumbent-baseline-v1.0.0",
            base_policy_version="v1.0.0",
            target_component=target_component,
            created_by=created_by,
            model_id="baseline-incumbent-model",
            model_version="1.0",
            tool_catalog_version="v1",
            a2_grant_version="v1",
            source_provenance_digest="0" * 64,
            proposal_diff={"type": "baseline"},
            prompt_templates={"system": "You are the baseline OpsBlaze incumbent agent."},
            active_lesson_ids=[],
            source_manifest_id=None,
            source_report_id=None,
        )
        return await self.seal_bundle(tenant_id=tenant_id, bundle_id=bundle.id)
