"""Deterministic Signature-Based Clustering for Feature E3."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.failure import (
    FailureCaseNotFoundError,
    FailureClassificationStatus,
    FailureClusterNotFoundError,
    FailureClusterStatus,
)
from backend.persistence.models import FailureCase, FailureCluster


class FailureClusterService:
    """Manages deterministic failure clustering, exemplar cases, and quarantine."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    @staticmethod
    def compute_signature_hash(
        tenant_id: uuid.UUID,
        failure_origin_code: str,
        first_divergent_step_category: str,
        subsystem_version: str,
    ) -> str:
        """Compute stable SHA-256 signature hash from deterministic 4-tuple."""
        sig = f"{tenant_id}:{failure_origin_code}:{first_divergent_step_category}:{subsystem_version}"
        return hashlib.sha256(sig.encode("utf-8")).hexdigest()

    async def assign_to_cluster(
        self,
        tenant_id: uuid.UUID,
        case_id: uuid.UUID,
        failure_origin_code: str,
        first_divergent_step_category: str = "UNKNOWN_CATEGORY",
        subsystem_version: str = "v1",
    ) -> FailureCluster:
        """Deterministically assigns a failure case to an active signature cluster."""
        sig_hash = self.compute_signature_hash(
            tenant_id=tenant_id,
            failure_origin_code=failure_origin_code,
            first_divergent_step_category=first_divergent_step_category,
            subsystem_version=subsystem_version,
        )

        if not self._session_factory:
            # Standalone / in-memory mode for pure unit tests
            return FailureCluster(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                signature_hash=sig_hash,
                failure_origin_code=failure_origin_code,
                subsystem_version=subsystem_version,
                first_divergent_step_category=first_divergent_step_category,
                cluster_status=FailureClusterStatus.ACTIVE.value,
                case_count=1,
                exemplar_case_id=case_id,
            )

        async with self._session_factory() as session:
            # Verify case exists for tenant and is not IGNORED (evaluation exclusion)
            case_check = (
                await session.execute(
                    select(FailureCase.classification_status).where(
                        FailureCase.id == case_id,
                        FailureCase.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if case_check is None:
                raise FailureCaseNotFoundError(f"Failure case '{case_id}' not found for tenant '{tenant_id}'.")
            if case_check == FailureClassificationStatus.IGNORED.value:
                raise ValueError("Cannot cluster an IGNORED evaluation case.")

            stmt = select(FailureCluster).where(
                FailureCluster.tenant_id == tenant_id,
                FailureCluster.failure_origin_code == failure_origin_code,
                FailureCluster.first_divergent_step_category == first_divergent_step_category,
                FailureCluster.subsystem_version == subsystem_version,
            )
            cluster = (await session.execute(stmt)).scalar_one_or_none()

            if cluster is None:
                cluster = FailureCluster(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    signature_hash=sig_hash,
                    failure_origin_code=failure_origin_code,
                    subsystem_version=subsystem_version,
                    first_divergent_step_category=first_divergent_step_category,
                    cluster_status=FailureClusterStatus.ACTIVE.value,
                    case_count=1,
                    exemplar_case_id=case_id,
                    version=1,
                )
                session.add(cluster)
            else:
                cluster.case_count += 1
                # Preserve existing exemplar_case_id if already set

            # Link failure case to cluster if the case row exists
            case_stmt = update(FailureCase).where(
                FailureCase.id == case_id,
                FailureCase.tenant_id == tenant_id,
            ).values(
                cluster_id=cluster.id,
                classification_status="CLUSTERED",
            )
            await session.execute(case_stmt)

            await session.commit()
            await session.refresh(cluster)
            return cluster

    async def get_cluster(
        self,
        tenant_id: uuid.UUID,
        cluster_id: uuid.UUID,
    ) -> FailureCluster:
        """Retrieves an existing cluster by ID within tenant isolation boundary."""
        if not self._session_factory:
            raise RuntimeError("Database session factory required for cluster operations.")

        async with self._session_factory() as session:
            stmt = select(FailureCluster).where(
                FailureCluster.tenant_id == tenant_id,
                FailureCluster.id == cluster_id,
            )
            cluster = (await session.execute(stmt)).scalar_one_or_none()
            if not cluster:
                raise FailureClusterNotFoundError(f"Cluster '{cluster_id}' not found for tenant '{tenant_id}'.")
            return cluster

    async def quarantine_cluster(
        self,
        tenant_id: uuid.UUID,
        cluster_id: uuid.UUID,
        quarantine_reason: str | None = None,
        reason: str | None = None,
        actor_id: str | None = None,
        actor_role: str | None = None,
    ) -> FailureCluster:
        """Quarantine a drifting or disputed failure cluster."""
        if not self._session_factory:
            raise RuntimeError("Database session factory required for cluster operations.")

        effective_reason = quarantine_reason or reason or "Unspecified quarantine reason"

        async with self._session_factory() as session:
            stmt = select(FailureCluster).where(
                FailureCluster.tenant_id == tenant_id,
                FailureCluster.id == cluster_id,
            )
            cluster = (await session.execute(stmt)).scalar_one_or_none()
            if not cluster:
                raise FailureClusterNotFoundError(f"Cluster '{cluster_id}' not found for tenant '{tenant_id}'.")

            cluster.cluster_status = FailureClusterStatus.QUARANTINED.value
            cluster.quarantine_reason = effective_reason
            await session.commit()
            await session.refresh(cluster)
            return cluster
