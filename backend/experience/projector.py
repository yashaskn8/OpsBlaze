"""Experience projector for OpsBlaze Feature E1 — Experience Capsule."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.experience import (
    CapsuleRedactionError,
    ExperienceCapsuleResponse,
    ExperienceClassification,
    ExperienceState,
)
from backend.experience.redact import REDACTION_VERSION, ExperienceRedactor
from backend.experience.repository import _to_response
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import (
    ConsentEpoch,
    EffectReceipt,
    EvidenceLedger,
    ExperienceCapsule,
    Incident,
    InvestigationStep,
    OutcomeRevision,
)

logger = logging.getLogger("opsblaze.experience.projector")


class ExperienceProjector:
    """Projects domain incident and investigation state into governed experience capsules."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def project_experience(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        *,
        step_id: uuid.UUID | None = None,
        schema_version: str = "v1",
        custom_summary: str | None = None,
        classification: ExperienceClassification | None = None,
        model_id: str = "titan20-investigator",
        model_version: str = "v1",
        evidence_manifest_sha256: str | None = None,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> ExperienceCapsuleResponse | None:
        """Idempotently project an experience capsule from server-side domain records.

        Guarantees:
        1. Non-blocking: Capture errors never fail or block the incident workflow.
        2. Idempotent: Repeated delivery of the same step produces the same logical capsule.
        3. Privacy-bounded: Redaction occurs before reusable storage.
        """
        current_time = now or datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                # 1. Fetch Incident
                inc = (
                    await session.execute(
                        select(Incident).where(
                            Incident.tenant_id == tenant_id, Incident.id == incident_id
                        )
                    )
                ).scalar_one_or_none()
                if inc is None:
                    logger.warning("Cannot project experience: Incident %s not found", incident_id)
                    return None

                # 2. Check Consent Epoch
                consent = (
                    await session.execute(
                        select(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id)
                    )
                ).scalar_one_or_none()
                consent_epoch = consent.current_epoch if consent else 1
                reuse_allowed = consent.reuse_allowed if consent else False

                # 3. Fetch Step & Model Info if available
                step: InvestigationStep | None = None
                if step_id is not None:
                    step = (
                        await session.execute(
                            select(InvestigationStep).where(
                                InvestigationStep.tenant_id == tenant_id,
                                InvestigationStep.id == step_id,
                            )
                        )
                    ).scalar_one_or_none()
                else:
                    # Pick latest step if exists
                    step = (
                        await session.execute(
                            select(InvestigationStep)
                            .where(
                                InvestigationStep.tenant_id == tenant_id,
                                InvestigationStep.incident_id == incident_id,
                            )
                            .order_by(InvestigationStep.step_no.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()

                chosen_model_id = model_id
                chosen_model_version = model_version
                raw_summary = (
                    custom_summary
                    if custom_summary is not None
                    else f"Incident {inc.id} on target {inc.target_id}"
                )

                if step is not None:
                    step_id = step.id
                    prompt_digest = step.prompt_digest
                    if step.proposed_plan_json and custom_summary is None:
                        plan_dict = step.proposed_plan_json
                        raw_summary += f"\nProposed Plan: {plan_dict.get('plan_id', 'unknown')}"
                else:
                    prompt_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

                # 4. Context Manifest Digest
                ev_count = (
                    await session.execute(
                        select(EvidenceLedger)
                        .where(
                            EvidenceLedger.tenant_id == tenant_id,
                            EvidenceLedger.incident_id == incident_id,
                        )
                        .limit(1)
                    )
                ).scalars().all()
                evidence_manifest_sha = evidence_manifest_sha256 or (prompt_digest if ev_count else "0" * 64)

                # 5. Fetch associated EffectReceipt if any
                rec = (
                    await session.execute(
                        select(EffectReceipt)
                        .where(
                            EffectReceipt.tenant_id == tenant_id,
                            EffectReceipt.incident_id == incident_id,
                        )
                        .order_by(EffectReceipt.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                receipt_id = rec.id if rec else None

                # 6. Fetch associated Outcome if any
                outcome = (
                    await session.execute(
                        select(OutcomeRevision)
                        .where(
                            OutcomeRevision.tenant_id == tenant_id,
                            OutcomeRevision.incident_id == incident_id,
                        )
                        .order_by(OutcomeRevision.revision_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                outcome_id = outcome.id if outcome else None
                outcome_rev_no = outcome.revision_no if outcome else None

                # 7. Classification
                if classification is not None:
                    effective_classification = classification
                else:
                    inc_status = inc.status_projection
                    if inc_status in ("RESOLVED", "CLOSED"):
                        effective_classification = ExperienceClassification.INVESTIGATION_SUCCESS
                    elif inc_status == "NEEDS_REVIEW":
                        effective_classification = ExperienceClassification.ABSTENTION
                    elif receipt_id is not None:
                        effective_classification = ExperienceClassification.CONSEQUENTIAL_ACTION
                    else:
                        effective_classification = ExperienceClassification.UNKNOWN

                # 8. Redact Payload
                is_quarantined = not reuse_allowed
                try:
                    redacted_summary, redacted_digest = ExperienceRedactor.redact_and_normalize(
                        raw_summary
                    )
                except CapsuleRedactionError as exc:
                    logger.warning("Capsule redaction failed, quarantining: %s", exc)
                    metrics.inc("experience_redaction_reject_total")
                    redacted_summary = "[REDACTION_FAILED: MALFORMED_PAYLOAD]"
                    redacted_digest = "0" * 64
                    is_quarantined = True

                lifecycle_state = (
                    ExperienceState.QUARANTINED.value
                    if is_quarantined
                    else ExperienceState.ELIGIBLE.value
                )

                # 9. Check existing capsule for idempotency
                existing = (
                    await session.execute(
                        select(ExperienceCapsule)
                        .where(
                            ExperienceCapsule.tenant_id == tenant_id,
                            ExperienceCapsule.incident_id == incident_id,
                            ExperienceCapsule.step_id == step_id,
                            ExperienceCapsule.schema_version == schema_version,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                if existing is not None:
                    # Never modify a tombstoned or purged capsule
                    if existing.lifecycle_state in (
                        ExperienceState.TOMBSTONED.value,
                        ExperienceState.PURGED.value,
                    ):
                        return _to_response(existing)

                    # Update associated outcome or receipt if new
                    if receipt_id and not existing.effect_receipt_id:
                        existing.effect_receipt_id = receipt_id
                    if outcome_id and not existing.outcome_id:
                        existing.outcome_id = outcome_id
                        existing.outcome_revision_no = outcome_rev_no
                    existing.updated_at = current_time
                    await session.commit()
                    return _to_response(existing)

                # 10. Insert New Capsule
                capsule_id = uuid.uuid4()
                effective_ttl = ttl if ttl is not None else timedelta(days=90)
                expires_at = current_time + effective_ttl

                row = ExperienceCapsule(
                    id=capsule_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    step_id=step_id,
                    schema_version=schema_version,
                    consent_epoch=consent_epoch,
                    policy_bundle_version="v1",
                    model_id=chosen_model_id,
                    model_version=chosen_model_version,
                    evidence_manifest_sha256=evidence_manifest_sha,
                    effect_receipt_id=receipt_id,
                    outcome_id=outcome_id,
                    outcome_revision_no=outcome_rev_no,
                    redaction_version=REDACTION_VERSION,
                    redacted_digest=redacted_digest,
                    summary_markdown=redacted_summary,
                    classification=effective_classification.value,
                    lifecycle_state=lifecycle_state,
                    occurred_at=inc.created_at,
                    expires_at=expires_at,
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(row)
                await session.commit()

            emit_event(
                "experience.projected",
                capsule_id=str(capsule_id),
                tenant_id=str(tenant_id),
                lifecycle_state=lifecycle_state,
            )
            metrics.inc("experience_projected_total")
            return _to_response(row)

        except Exception:
            logger.exception(
                "Non-blocking error during experience projection for incident %s",
                incident_id,
            )
            metrics.inc("experience_projection_error_total")
            return None
