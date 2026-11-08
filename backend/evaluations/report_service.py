"""Candidate Evaluation Report & Human Review Service for Feature E5.

Responsible for:
- Gathering completed paired trial records for a manifest.
- Re-checking underlying source eligibility (INV-E5-9).
- Evaluating the 6 pre-specified statistical quality and safety gates.
- Generating and persisting CandidateEvaluationReport records.
- Synchronous query-time dynamic invalidation on outcome or consent supersession.
- Enforcing CAS optimistic concurrency and authorized human operator roles on review sign-off.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.evaluation import (
    EvaluationConcurrencyConflictError,
    ManifestLifecycleState,
    ManifestNotFoundError,
    MetricSpec,
    ReportAlreadyReviewedError,
    ReportNotFoundError,
    ReviewDecision,
    SourceEligibilityInvalidatedError,
    TrialStatus,
    evaluate_statistical_gates,
)
from backend.persistence.models import (
    CandidateEvaluationManifest,
    CandidateEvaluationReport,
    CandidateEvaluationTrial,
    ConsentEpoch,
    OutcomeRevision,
)

logger = logging.getLogger("opsblaze.evaluations.report_service")


class CandidateReportService:
    """Manages report compilation, dynamic invalidation, and human review decisions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def check_source_eligibility(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        manifest: CandidateEvaluationManifest,
    ) -> tuple[bool, str | None]:
        """Verifies that underlying source revisions and consent epochs remain active and valid (INV-E5-9)."""
        # 1. Check Tenant Consent Epoch
        epoch = await session.get(ConsentEpoch, tenant_id)
        if epoch and not epoch.reuse_allowed:
            return False, "Tenant consent epoch revoked: reuse_allowed is False"

        # 2. Check Candidate Bundle Lesson Epoch
        cand_bundle = manifest.candidate_bundle or {}
        lesson_epoch = cand_bundle.get("active_lesson_epoch")
        if epoch and lesson_epoch is not None and epoch.current_epoch != lesson_epoch:
            return False, f"Lesson epoch mismatch: current is {epoch.current_epoch}, bundle specified {lesson_epoch}"

        # 3. Check Source Outcome Revisions are not superseded
        source_rev_ids = manifest.source_outcome_revision_ids or []
        for rid_str in source_rev_ids:
            try:
                rid = uuid.UUID(str(rid_str))
            except (ValueError, TypeError):
                continue
            rev = await session.scalar(
                select(OutcomeRevision).where(
                    OutcomeRevision.tenant_id == tenant_id,
                    OutcomeRevision.id == rid,
                )
            )
            if not rev:
                return False, f"Source outcome revision {rid} does not exist for tenant"

            # Check if a newer revision exists for the same incident
            newer_rev = await session.scalar(
                select(OutcomeRevision.id).where(
                    OutcomeRevision.tenant_id == tenant_id,
                    OutcomeRevision.incident_id == rev.incident_id,
                    OutcomeRevision.revision_no > rev.revision_no,
                )
            )
            if newer_rev:
                return False, f"Source outcome revision {rid} was superseded by revision {newer_rev}"

            # Check if disputed
            if rev.dispute_reason:
                return False, f"Source outcome revision {rid} is currently disputed: {rev.dispute_reason}"

        return True, None

    async def generate_report(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest_id: uuid.UUID,
    ) -> CandidateEvaluationReport:
        """Gathers completed trials, evaluates statistical gates, and persists report."""
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == manifest_id,
                )
            )
            if not manifest:
                raise ManifestNotFoundError(f"Manifest {manifest_id} not found")

            # Check source eligibility
            source_active, inval_reason = await self.check_source_eligibility(session, tenant_id, manifest)

            # Query all trials for this manifest
            trials = (
                await session.scalars(
                    select(CandidateEvaluationTrial).where(
                        CandidateEvaluationTrial.manifest_id == manifest_id
                    )
                )
            ).all()

            # Group trials by pair_id
            pairs_dict: dict[uuid.UUID, dict[str, Any]] = {}
            for t in trials:
                if t.pair_id not in pairs_dict:
                    pairs_dict[t.pair_id] = {}
                pairs_dict[t.pair_id][t.arm.lower()] = {
                    "trial_status": t.trial_status,
                    "validity_state": t.validity_state,
                    "decision_correct": t.decision_correct,
                    "outcome_label": t.outcome_label,
                    "probes_count": t.probes_count,
                    "tokens_consumed": t.tokens_consumed,
                    "cost_micros": t.cost_micros,
                    "duration_ms": t.duration_ms,
                    "is_negative_control": t.is_negative_control,
                    "unsafe_action_detected": t.unsafe_action_detected,
                    "unsafe_action_count": t.unsafe_action_count,
                }
                if t.is_negative_control:
                    pairs_dict[t.pair_id]["is_negative_control"] = True

            # Separate into valid completed pairs vs invalid pairs
            completed_pairs: list[dict[str, Any]] = []
            invalid_pairs_count = 0

            for p_id, p_arms in pairs_dict.items():
                inc = p_arms.get("incumbent")
                cand = p_arms.get("candidate")

                if (
                    inc
                    and cand
                    and inc["trial_status"] == TrialStatus.COMPLETED.value
                    and cand["trial_status"] == TrialStatus.COMPLETED.value
                    and inc["validity_state"] == "VALID"
                    and cand["validity_state"] == "VALID"
                ):
                    completed_pairs.append(
                        {
                            "pair_id": p_id,
                            "is_negative_control": p_arms.get("is_negative_control", False),
                            "incumbent": inc,
                            "candidate": cand,
                        }
                    )
                else:
                    invalid_pairs_count += 1

            metric_spec = MetricSpec.model_validate(manifest.metric_spec)
            report_status, promotion_eligible, summary, verdicts = evaluate_statistical_gates(
                pairs_planned=manifest.sample_size_plan,
                completed_pairs=completed_pairs,
                invalid_pairs_count=invalid_pairs_count,
                metric_spec=metric_spec,
                source_eligibility_active=source_active,
            )

            # Persist Report
            report_id = uuid.uuid4()
            report = CandidateEvaluationReport(
                id=report_id,
                tenant_id=tenant_id,
                manifest_id=manifest_id,
                report_status=report_status.value,
                promotion_eligible=promotion_eligible,
                invalidation_reason=inval_reason if not source_active else None,
                total_pairs_planned=manifest.sample_size_plan,
                total_pairs_completed=len(completed_pairs),
                invalid_pairs_count=invalid_pairs_count,
                metrics_summary=summary,
                mcnemar_statistic=summary.get("mcnemar_statistic"),
                mcnemar_p_value=summary.get("mcnemar_p_value"),
                recovery_rate_incumbent=summary["incumbent_recovery_rate"],
                recovery_rate_candidate=summary["candidate_recovery_rate"],
                recovery_delta=summary["recovery_delta"],
                recovery_ci_low=summary["ci_low"],
                recovery_ci_high=summary["ci_high"],
                cost_per_verified_incumbent=summary["cost_per_verified_incumbent"],
                cost_per_verified_candidate=summary["cost_per_verified_candidate"],
                negative_control_pass_rate=summary["negative_control_pass_rate"],
                unsafe_action_count=summary["unsafe_action_count"],
                gate_verdicts=[v.model_dump() for v in verdicts],
                version=1,
            )
            session.add(report)

            # Update manifest lifecycle to COMPLETED
            manifest.lifecycle_state = ManifestLifecycleState.COMPLETED.value
            manifest.version += 1
            manifest.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(report)
            return report

    async def get_latest_report(
        self,
        *,
        tenant_id: uuid.UUID,
        manifest_id: uuid.UUID,
    ) -> CandidateEvaluationReport | None:
        """Retrieves the latest evaluation report with synchronous query-time invalidation (INV-E5-9)."""
        async with self._session_factory() as session:
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == manifest_id,
                )
            )
            if not manifest:
                raise ManifestNotFoundError(f"Manifest {manifest_id} not found")

            report = await session.scalar(
                select(CandidateEvaluationReport)
                .where(
                    CandidateEvaluationReport.tenant_id == tenant_id,
                    CandidateEvaluationReport.manifest_id == manifest_id,
                )
                .order_by(CandidateEvaluationReport.version.desc())
            )
            if not report:
                return None

            # Dynamic Invalidation Check (INV-E5-9)
            source_active, inval_reason = await self.check_source_eligibility(session, tenant_id, manifest)
            if not source_active and report.promotion_eligible:
                # Synchronously invalidate report in DB and in-memory
                report.promotion_eligible = False
                report.invalidation_reason = inval_reason
                report.updated_at = datetime.now(UTC)
                await session.commit()

            return report

    async def submit_review(
        self,
        *,
        tenant_id: uuid.UUID,
        report_id: uuid.UUID,
        reviewer_id: str,
        reviewer_role: str,
        decision: ReviewDecision | str,
        comments: str = "",
        expected_version: int = 1,
    ) -> CandidateEvaluationReport:
        """Submits human evaluation reviewer decision enforcing CAS optimistic concurrency."""
        # 1. Validate Reviewer Role (reject model accounts)
        valid_role = validate_reviewer_role(reviewer_role)
        norm_decision = ReviewDecision(decision).value

        async with self._session_factory() as session:
            report = await session.scalar(
                select(CandidateEvaluationReport).where(
                    CandidateEvaluationReport.tenant_id == tenant_id,
                    CandidateEvaluationReport.id == report_id,
                )
            )
            if not report:
                raise ReportNotFoundError(f"Report {report_id} not found")

            if report.review_decision is not None:
                raise ReportAlreadyReviewedError(
                    f"Report {report_id} has already been reviewed ({report.review_decision})"
                )

            # 2. Re-verify source eligibility
            manifest = await session.scalar(
                select(CandidateEvaluationManifest).where(
                    CandidateEvaluationManifest.tenant_id == tenant_id,
                    CandidateEvaluationManifest.id == report.manifest_id,
                )
            )
            if manifest:
                source_active, reason = await self.check_source_eligibility(session, tenant_id, manifest)
                if not source_active:
                    raise SourceEligibilityInvalidatedError(
                        f"Cannot review report: underlying source evidence has been invalidated ({reason})"
                    )

            # 3. Execute atomic CAS Update on Report
            res = await session.execute(
                update(CandidateEvaluationReport)
                .where(
                    CandidateEvaluationReport.tenant_id == tenant_id,
                    CandidateEvaluationReport.id == report_id,
                    CandidateEvaluationReport.version == expected_version,
                    CandidateEvaluationReport.review_decision.is_(None),
                )
                .values(
                    reviewer_id=reviewer_id,
                    reviewer_role=valid_role,
                    review_decision=norm_decision,
                    review_comments=comments,
                    source_eligibility_rechecked=True,
                    reviewed_at=datetime.now(UTC),
                    version=report.version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            rowcount = getattr(res, "rowcount", -1)
            if rowcount == 0:
                raise EvaluationConcurrencyConflictError(
                    f"Report {report_id} CAS conflict: expected version {expected_version}, found {report.version}"
                )

            await session.commit()
            await session.refresh(report)
            return report
