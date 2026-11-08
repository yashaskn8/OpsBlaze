"""Outcome Adjudication Service for OpsBlaze Feature E2 — Independent Outcome Adjudication Ledger."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.experience import ExperienceClassification, ExperienceState
from backend.domain.incident import IncidentNotFoundError
from backend.domain.outcome import (
    ConfidenceBand,
    ObservationCreateRequest,
    ObservationResponse,
    OperatorFeedbackRequest,
    OutcomeConflictError,
    OutcomeLabel,
    OutcomeRevisionResponse,
    OutcomeSummaryResponse,
    SelfGradingForbiddenError,
    SourceKind,
    compute_observation_digest,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import (
    ExperienceCapsule,
    Incident,
    OutcomeObservation,
    OutcomeRevision,
)

logger = logging.getLogger("opsblaze.outcomes.service")


def _to_observation_response(row: OutcomeObservation) -> ObservationResponse:
    return ObservationResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        incident_id=row.incident_id,
        source_kind=SourceKind(row.source_kind),
        source_reference=row.source_reference,
        rule_version=row.rule_version,
        result_payload=row.result_payload,
        source_digest=row.source_digest,
        idempotency_key=row.idempotency_key,
        observed_at=row.observed_at,
        created_at=row.created_at,
    )


def _to_revision_response(row: OutcomeRevision) -> OutcomeRevisionResponse:
    return OutcomeRevisionResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        incident_id=row.incident_id,
        revision_no=row.revision_no,
        rule_version=row.rule_version,
        label=OutcomeLabel(row.label),
        confidence_band=ConfidenceBand(row.confidence_band),
        dispute_reason=row.dispute_reason,
        prior_revision_id=row.prior_revision_id,
        reviewer_id=row.reviewer_id,
        reviewer_role=row.reviewer_role,
        supporting_observation_ids=row.supporting_observation_ids,
        adjudication_metadata=row.adjudication_metadata,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


class OutcomeService:
    """Manages append-only outcome observations, rule adjudication, and human dispute resolution."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_observation(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        request: ObservationCreateRequest,
        *,
        now: datetime | None = None,
    ) -> tuple[ObservationResponse, OutcomeRevisionResponse]:
        """Record an append-only observation and trigger deterministic outcome adjudication."""
        current_time = now or datetime.now(UTC)
        obs_time = request.observed_at or current_time

        # Reject autonomous model self-grading
        if request.source_kind == SourceKind.MODEL_ANNOTATION:
            status_val = str(request.result_payload.get("status", "")).upper()
            if status_val in ("SUCCESS", "PASS", "RESOLVED") or request.result_payload.get("success") is True:
                metrics.inc("outcome_self_grading_blocked_total")
                raise SelfGradingForbiddenError()

        digest = compute_observation_digest(
            source_kind=request.source_kind.value,
            source_reference=request.source_reference,
            rule_version=request.rule_version,
            result_payload=request.result_payload,
        )

        async with self._session_factory() as session:
            # 1. Verify incident exists
            inc = (
                await session.execute(
                    select(Incident).where(
                        Incident.tenant_id == tenant_id, Incident.id == incident_id
                    )
                )
            ).scalar_one_or_none()
            if inc is None:
                raise IncidentNotFoundError(incident_id)

            # 2. Idempotent check for observation
            existing_obs = (
                await session.execute(
                    select(OutcomeObservation).where(
                        OutcomeObservation.tenant_id == tenant_id,
                        OutcomeObservation.incident_id == incident_id,
                        OutcomeObservation.idempotency_key == request.idempotency_key,
                    )
                )
            ).scalar_one_or_none()

            if existing_obs is not None:
                obs_row = existing_obs
            else:
                obs_row = OutcomeObservation(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    source_kind=request.source_kind.value,
                    source_reference=request.source_reference,
                    observed_at=obs_time,
                    rule_version=request.rule_version,
                    result_payload=request.result_payload,
                    source_digest=digest,
                    idempotency_key=request.idempotency_key,
                    created_at=current_time,
                )
                session.add(obs_row)
                await session.flush()
                metrics.inc("outcome_observation_recorded_total")

            # 3. Fetch all observations for incident to adjudicate
            all_obs = (
                (
                    await session.execute(
                        select(OutcomeObservation)
                        .where(
                            OutcomeObservation.tenant_id == tenant_id,
                            OutcomeObservation.incident_id == incident_id,
                        )
                        .order_by(OutcomeObservation.observed_at.asc())
                    )
                )
                .scalars()
                .all()
            )

            # 4. Fetch latest revision
            latest_rev = (
                await session.execute(
                    select(OutcomeRevision)
                    .where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.incident_id == incident_id,
                    )
                    .order_by(OutcomeRevision.revision_no.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            # 5. Adjudication Rule Engine
            new_label, new_confidence, is_dispute, dispute_msg = self._evaluate_adjudication_rules(
                all_obs, latest_rev
            )

            # If revision needs to be created
            should_create_revision = False
            if latest_rev is None:
                should_create_revision = True
                next_rev_no = 1
                prior_id = None
            elif (
                latest_rev.label != new_label.value
                or latest_rev.confidence_band != new_confidence.value
                or (is_dispute and latest_rev.label != OutcomeLabel.DISPUTED.value)
            ):
                should_create_revision = True
                next_rev_no = latest_rev.revision_no + 1
                prior_id = latest_rev.id
            else:
                next_rev_no = latest_rev.revision_no
                prior_id = latest_rev.prior_revision_id

            if should_create_revision:
                supporting_ids = [str(o.id) for o in all_obs]
                rev_row = OutcomeRevision(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    revision_no=next_rev_no,
                    rule_version=request.rule_version,
                    label=new_label.value,
                    confidence_band=new_confidence.value,
                    dispute_reason=dispute_msg if is_dispute else None,
                    prior_revision_id=prior_id,
                    reviewer_id=None,
                    reviewer_role=None,
                    supporting_observation_ids=supporting_ids,
                    adjudication_metadata={
                        "observation_count": len(all_obs),
                        "trigger_observation_id": str(obs_row.id),
                    },
                    decided_at=current_time,
                    created_at=current_time,
                )
                session.add(rev_row)
                await session.flush()
                metrics.inc(f"outcome_{new_label.value.lower()}_total")

                # Synchronize linked experience capsules
                await self._sync_dependent_capsules(
                    session, tenant_id, incident_id, rev_row, current_time
                )
            else:
                assert latest_rev is not None
                rev_row = latest_rev

            await session.commit()

        emit_event(
            "outcome.adjudicated",
            tenant_id=str(tenant_id),
            incident_id=str(incident_id),
            revision_no=rev_row.revision_no,
            label=rev_row.label,
        )
        return _to_observation_response(obs_row), _to_revision_response(rev_row)

    def _evaluate_adjudication_rules(
        self,
        observations: Sequence[OutcomeObservation],
        latest_rev: OutcomeRevision | None,
    ) -> tuple[OutcomeLabel, ConfidenceBand, bool, str | None]:
        """Deterministic adjudication logic decoupling adapter ACKs from independent checks."""
        # If already human-revised and no new conflicting automated signals, retain human revision
        if latest_rev is not None and latest_rev.reviewer_id is not None:
            # Human resolution holds unless new observation indicates failure relapse
            latest_obs = observations[-1] if observations else None
            if latest_obs and latest_obs.source_kind in (
                SourceKind.INDEPENDENT_WORKLOAD_CHECK.value,
                SourceKind.DELAYED_MONITOR.value,
            ):
                payload = latest_obs.result_payload
                status_str = str(payload.get("status", "")).upper()
                if status_str in ("FAIL", "FAILED", "ERROR") or payload.get("success") is False:
                    return (
                        OutcomeLabel.DISPUTED,
                        ConfidenceBand.LOW,
                        True,
                        f"Relapse detected post-human-review by {latest_obs.source_reference}",
                    )
            return (
                OutcomeLabel(latest_rev.label),
                ConfidenceBand(latest_rev.confidence_band),
                latest_rev.label == OutcomeLabel.DISPUTED.value,
                latest_rev.dispute_reason,
            )

        independent_pass = False
        independent_fail = False
        has_adapter_ack = False
        delayed_relapse = False

        for obs in observations:
            kind = obs.source_kind
            payload = obs.result_payload
            status_str = str(payload.get("status", "")).upper()
            is_success = (status_str in ("PASS", "SUCCESS", "OK")) or (
                payload.get("success") is True
            )
            is_failure = (status_str in ("FAIL", "FAILED", "ERROR")) or (
                payload.get("success") is False
            )

            if kind == SourceKind.ADAPTER_ACK.value:
                has_adapter_ack = True
            elif kind == SourceKind.INDEPENDENT_WORKLOAD_CHECK.value:
                if is_success:
                    independent_pass = True
                elif is_failure:
                    independent_fail = True
            elif kind == SourceKind.DELAYED_MONITOR.value and is_failure:
                delayed_relapse = True

        # Rule 1: Conflicting evidence or relapse -> DISPUTED
        if delayed_relapse and (independent_pass or has_adapter_ack):
            return (
                OutcomeLabel.DISPUTED,
                ConfidenceBand.LOW,
                True,
                "Delayed monitor reported relapse after apparent resolution",
            )
        if independent_pass and independent_fail:
            return (
                OutcomeLabel.DISPUTED,
                ConfidenceBand.LOW,
                True,
                "Conflicting independent workload check results observed",
            )

        # Rule 2: Independent check fails -> VERIFIED_FAILURE (0% false success tolerance)
        if independent_fail:
            return OutcomeLabel.VERIFIED_FAILURE, ConfidenceBand.HIGH, False, None

        # Rule 3: Independent check passes -> VERIFIED_SUCCESS
        if independent_pass:
            return OutcomeLabel.VERIFIED_SUCCESS, ConfidenceBand.HIGH, False, None

        # Rule 4: Adapter ACK only or model annotation only -> OBSERVED (cannot claim verified success)
        if has_adapter_ack:
            return OutcomeLabel.OBSERVED, ConfidenceBand.LOW, False, None

        # Default fallback
        return OutcomeLabel.PENDING, ConfidenceBand.LOW, False, None

    async def submit_operator_feedback(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        request: OperatorFeedbackRequest,
        *,
        now: datetime | None = None,
    ) -> OutcomeRevisionResponse:
        """Human reviewer dispute resolution or manual verdict with CAS optimistic locking."""
        validate_reviewer_role(request.actor_role)
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            # 1. Fetch latest revision with lock
            latest_rev = (
                await session.execute(
                    select(OutcomeRevision)
                    .where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.incident_id == incident_id,
                    )
                    .order_by(OutcomeRevision.revision_no.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            current_rev_no = latest_rev.revision_no if latest_rev else 0
            if current_rev_no != request.expected_revision_no:
                raise OutcomeConflictError(request.expected_revision_no, current_rev_no)

            # 2. Record human observation
            obs_id = uuid.uuid4()
            obs_payload = {
                "verdict": request.label.value,
                "reason": request.reason,
                "actor_id": request.actor_id,
                "actor_role": request.actor_role,
            }
            obs_digest = compute_observation_digest(
                SourceKind.OPERATOR_FEEDBACK.value,
                f"user:{request.actor_id}",
                "v1",
                obs_payload,
            )
            obs_row = OutcomeObservation(
                id=obs_id,
                tenant_id=tenant_id,
                incident_id=incident_id,
                source_kind=SourceKind.OPERATOR_FEEDBACK.value,
                source_reference=f"user:{request.actor_id}",
                observed_at=current_time,
                rule_version="v1",
                result_payload=obs_payload,
                source_digest=obs_digest,
                idempotency_key=f"feedback-{obs_id}",
                created_at=current_time,
            )
            session.add(obs_row)

            # 3. Append new revision
            next_rev_no = current_rev_no + 1
            prior_id = latest_rev.id if latest_rev else None
            supporting_ids = [str(obs_id)]
            if latest_rev:
                supporting_ids.extend(latest_rev.supporting_observation_ids)

            rev_row = OutcomeRevision(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                incident_id=incident_id,
                revision_no=next_rev_no,
                rule_version="v1",
                label=request.label.value,
                confidence_band=ConfidenceBand.HIGH.value,
                dispute_reason=request.reason,
                prior_revision_id=prior_id,
                reviewer_id=request.actor_id,
                reviewer_role=request.actor_role,
                supporting_observation_ids=supporting_ids,
                adjudication_metadata={
                    "resolution_type": "OPERATOR_FEEDBACK",
                    "actor_role": request.actor_role,
                },
                decided_at=current_time,
                created_at=current_time,
            )
            session.add(rev_row)
            await session.flush()

            # 4. Synchronize dependent capsules
            await self._sync_dependent_capsules(
                session, tenant_id, incident_id, rev_row, current_time
            )
            await session.commit()

        emit_event(
            "outcome.operator_feedback",
            tenant_id=str(tenant_id),
            incident_id=str(incident_id),
            revision_no=rev_row.revision_no,
            label=rev_row.label,
            actor_id=request.actor_id,
        )
        metrics.inc("outcome_operator_feedback_total")
        return _to_revision_response(rev_row)

    async def _sync_dependent_capsules(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        rev_row: OutcomeRevision,
        current_time: datetime,
    ) -> None:
        """Propagate outcome revision updates and dispute status to linked experience capsules."""
        capsules = (
            (
                await session.execute(
                    select(ExperienceCapsule)
                    .where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.incident_id == incident_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

        for cap in capsules:
            # Do not touch tombstoned or purged records
            if cap.lifecycle_state in (
                ExperienceState.TOMBSTONED.value,
                ExperienceState.PURGED.value,
            ):
                continue

            cap.outcome_id = rev_row.id
            cap.outcome_revision_no = rev_row.revision_no
            cap.updated_at = current_time

            # Invalidate/quarantine capsule if outcome was disputed or marked failed
            if (
                rev_row.label
                in (
                    OutcomeLabel.DISPUTED.value,
                    OutcomeLabel.VERIFIED_FAILURE.value,
                )
                and cap.lifecycle_state == ExperienceState.ELIGIBLE.value
            ):
                cap.lifecycle_state = ExperienceState.QUARANTINED.value
                if rev_row.label == OutcomeLabel.VERIFIED_FAILURE.value:
                    cap.classification = ExperienceClassification.INVESTIGATION_FAILURE.value
                logger.info(
                    "Quarantined dependent capsule %s due to outcome %s",
                    cap.id,
                    rev_row.label,
                )

    async def get_outcome_summary(
        self, tenant_id: uuid.UUID, incident_id: uuid.UUID
    ) -> OutcomeSummaryResponse:
        """Fetch complete outcome history, latest revision, and dispute indicators."""
        async with self._session_factory() as session:
            # 1. Fetch latest revision
            latest_rev = (
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

            # 2. Fetch observations
            obs_rows = (
                (
                    await session.execute(
                        select(OutcomeObservation)
                        .where(
                            OutcomeObservation.tenant_id == tenant_id,
                            OutcomeObservation.incident_id == incident_id,
                        )
                        .order_by(OutcomeObservation.observed_at.asc())
                    )
                )
                .scalars()
                .all()
            )

            if latest_rev is None and not obs_rows:
                # Check if incident exists
                inc = (
                    await session.execute(
                        select(Incident).where(
                            Incident.tenant_id == tenant_id, Incident.id == incident_id
                        )
                    )
                ).scalar_one_or_none()
                if inc is None:
                    raise IncidentNotFoundError(incident_id)

            is_disputed = False
            if latest_rev and latest_rev.label == OutcomeLabel.DISPUTED.value:
                is_disputed = True

            return OutcomeSummaryResponse(
                incident_id=incident_id,
                tenant_id=tenant_id,
                current_revision=_to_revision_response(latest_rev) if latest_rev else None,
                is_disputed=is_disputed,
                total_observations=len(obs_rows),
                observations=[_to_observation_response(r) for r in obs_rows],
            )
