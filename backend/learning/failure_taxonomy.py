"""Deterministic First-Stage Failure Classifier for Feature E3."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.failure import (
    DivergentStepCategory,
    FailureClassificationStatus,
    FailureOriginCode,
)
from backend.persistence.models import FailureCase


class FailureClassifier:
    """Classifies incident outcomes and evidence into structured, typed failure cases."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    async def classify_case(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        outcome_revision_id: uuid.UUID,
        outcome_label: str,
        divergent_step: str | None = None,
        first_divergent_step_category: str | None = None,
        subsystem_version: str = "v1",
        is_evaluation: bool = False,
        capsule_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> FailureCase:
        """Deterministically classifies an incident failure from attributable evidence."""
        det = details or {}
        step_cat = first_divergent_step_category or DivergentStepCategory.UNKNOWN_CATEGORY.value

        # 1. INV-E3-5: Evaluation & negative control exclusion
        if is_evaluation:
            origin_code = FailureOriginCode.UNCLASSIFIED.value
            status = FailureClassificationStatus.IGNORED.value
        # 2. Categorize by specific step category where evidenced
        elif step_cat == DivergentStepCategory.AUTHORIZATION_DENIED.value:
            origin_code = FailureOriginCode.TOOL_AUTHORIZATION_DENIAL.value
            status = FailureClassificationStatus.CLASSIFIED.value
        elif step_cat == DivergentStepCategory.FENCE_REJECTED.value:
            origin_code = FailureOriginCode.RESOURCE_FENCING_FAILURE.value
            status = FailureClassificationStatus.CLASSIFIED.value
        elif step_cat == DivergentStepCategory.BUDGET_EXCEEDED.value:
            origin_code = FailureOriginCode.BUDGET_ADMISSION_CONSTRAINT.value
            status = FailureClassificationStatus.CLASSIFIED.value
        # 3. Categorize by E2 Outcome Revision Label
        elif outcome_label == "VERIFIED_FAILURE":
            origin_code = FailureOriginCode.VERIFIED_FAILURE.value
            status = FailureClassificationStatus.CLASSIFIED.value
        elif outcome_label == "DISPUTED":
            origin_code = FailureOriginCode.DISPUTED_OUTCOME.value
            status = FailureClassificationStatus.UNCERTAIN.value
        elif outcome_label in ("PENDING", "OBSERVED", "UNKNOWN"):
            origin_code = FailureOriginCode.UNCERTAIN_EVIDENCE.value
            status = FailureClassificationStatus.UNCERTAIN.value
        else:
            origin_code = FailureOriginCode.UNCLASSIFIED.value
            status = FailureClassificationStatus.UNCERTAIN.value

        # Compute stable deterministic evidence digest
        digest_input = f"{tenant_id}:{incident_id}:{outcome_revision_id}:{outcome_label}:{step_cat}:{subsystem_version}"
        evidence_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

        case = FailureCase(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=incident_id,
            outcome_revision_id=outcome_revision_id,
            capsule_id=capsule_id,
            failure_origin_code=origin_code,
            subsystem_version=subsystem_version,
            first_divergent_step_category=step_cat,
            first_divergent_step=divergent_step,
            classification_status=status,
            classification_version="v1",
            evidence_digest=evidence_digest,
            details=det,
        )

        # If a session factory is available, persist the case with idempotency
        if self._session_factory:
            async with self._session_factory() as session:
                # Check for existing case for this tenant and outcome revision
                stmt = select(FailureCase).where(
                    FailureCase.tenant_id == tenant_id,
                    FailureCase.outcome_revision_id == outcome_revision_id,
                )
                existing = (await session.execute(stmt)).scalar_one_or_none()
                if existing:
                    return existing

                session.add(case)
                try:
                    await session.commit()
                    await session.refresh(case)
                except Exception:
                    await session.rollback()
                    # Re-query on race
                    existing = (await session.execute(stmt)).scalar_one_or_none()
                    if existing:
                        return existing
                    raise

        return case
