"""Feature E6 — Release Safety Guardrails and Automated Rollback Service.

Evaluates active CANARY and SHADOW releases against two tiers of safety guardrails:
1. Class 1 (Zero-Tolerance Invariants):
   - N = 1 trigger on unauthorized mutations, negative-control breaches, or B2 fence violations.
   - Sourced strictly from trusted server-side evidence and audit logs.
   - Triggers instantaneous automated rollback.
2. Class 2 (Statistical Rate Metrics):
   - Evaluated after minimum sample size N_min >= 30.
   - Triggers automated rollback if Candidate Error Rate > 5% and Relative Risk (RR) vs concurrent Incumbent > 1.5.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    CanaryArm,
    CanaryMetricsResponse,
    ReleaseState,
)
from backend.persistence.models import Incident, PolicyRelease, ReleaseEventAudit
from backend.releases.release_service import PolicyReleaseService

logger = logging.getLogger("opsblaze.releases.guardrails")


class ReleaseGuardrailService:
    """Monitors live canary metrics and executes automated rollbacks upon threshold breaches."""

    MIN_CANARY_SAMPLE_SIZE = 30
    MAX_ALLOWED_ERROR_RATE = 0.05  # 5%
    MAX_ALLOWED_RELATIVE_RISK = 1.5

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        release_service: PolicyReleaseService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._release_service = release_service or PolicyReleaseService(session_factory)

    async def record_invariant_breach(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        breach_detail: str,
        actor_id: str = "system",
        actor_role: str = "system",
    ) -> ReleaseEventAudit:
        """Records an authoritative Class 1 invariant breach event in the audit trail."""
        async with self._session_factory() as session:
            rel = await session.get(PolicyRelease, release_id)
            current_state = rel.state if rel else "CANARY"
            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type="INVARIANT_BREACH",
                from_state=current_state,
                to_state=current_state,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={"detail": breach_detail},
                created_at=datetime.now(UTC),
            )
            session.add(audit)
            await session.commit()
            return audit

    async def compute_canary_metrics(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        test_invariant_breaches: int = 0,
    ) -> CanaryMetricsResponse:
        """Calculates error rates, sample sizes, and relative risk for active canary."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ValueError(f"Release {release_id} not found.")

            # Count real server-side invariant breaches
            stmt_breaches = (
                select(func.count(ReleaseEventAudit.id))
                .where(
                    ReleaseEventAudit.tenant_id == tenant_id,
                    ReleaseEventAudit.release_id == release_id,
                    ReleaseEventAudit.event_type.in_(["INVARIANT_BREACH", "UNAUTHORIZED_MUTATION", "B2_FENCE_VIOLATION"]),
                )
            )
            real_breaches = (await session.execute(stmt_breaches)).scalar_one_or_none() or 0
            total_invariant_breaches = real_breaches + test_invariant_breaches

            stmt_latest_breach = (
                select(ReleaseEventAudit)
                .where(
                    ReleaseEventAudit.tenant_id == tenant_id,
                    ReleaseEventAudit.release_id == release_id,
                    ReleaseEventAudit.event_type.in_(["INVARIANT_BREACH", "UNAUTHORIZED_MUTATION", "B2_FENCE_VIOLATION"]),
                )
                .order_by(ReleaseEventAudit.created_at.desc())
            )
            latest_breach_audit = (await session.execute(stmt_latest_breach)).scalars().first()
            audit_detail = (latest_breach_audit.metadata_redacted or {}).get("detail") if latest_breach_audit else None

            # Count total candidate incidents and failures
            stmt_cand = (
                select(
                    func.count(Incident.id).label("total"),
                    func.count(Incident.id).filter(
                        Incident.status_projection.in_(["FAILED", "TIMED_OUT", "DEAD_LETTER", "UNRESOLVED"])
                    ).label("failed"),
                )
                .where(
                    Incident.tenant_id == tenant_id,
                    Incident.policy_release_id == release_id,
                    Incident.canary_arm == CanaryArm.CANDIDATE.value,
                )
            )
            cand_res = (await session.execute(stmt_cand)).one()
            cand_total = cand_res.total or 0
            cand_failed = cand_res.failed or 0

            # Count concurrent incumbent incidents and failures
            stmt_inc = (
                select(
                    func.count(Incident.id).label("total"),
                    func.count(Incident.id).filter(
                        Incident.status_projection.in_(["FAILED", "TIMED_OUT", "DEAD_LETTER", "UNRESOLVED"])
                    ).label("failed"),
                )
                .where(
                    Incident.tenant_id == tenant_id,
                    Incident.policy_release_id == release_id,
                    Incident.canary_arm == CanaryArm.INCUMBENT.value,
                )
            )
            inc_res = (await session.execute(stmt_inc)).one()
            inc_total = inc_res.total or 0
            inc_failed = inc_res.failed or 0

            cand_error_rate = (cand_failed / cand_total) if cand_total > 0 else 0.0
            inc_error_rate = (inc_failed / inc_total) if inc_total > 0 else 0.0

            if inc_error_rate > 0.0:
                relative_risk = round(cand_error_rate / inc_error_rate, 4)
            elif cand_error_rate > 0.0:
                relative_risk = 999.0
            else:
                relative_risk = 1.0

            recommendation = "HEALTHY"
            if total_invariant_breaches > 0:
                recommendation = "ROLLBACK_REQUIRED_INVARIANT_BREACH"
            elif (
                cand_total >= self.MIN_CANARY_SAMPLE_SIZE
                and cand_error_rate > self.MAX_ALLOWED_ERROR_RATE
                and relative_risk > self.MAX_ALLOWED_RELATIVE_RISK
            ):
                recommendation = "ROLLBACK_REQUIRED_STATISTICAL_RATE"
            elif cand_total < self.MIN_CANARY_SAMPLE_SIZE:
                recommendation = "INSUFFICIENT_SAMPLE_SIZE"

            return CanaryMetricsResponse(
                release_id=release.id,
                target_component=release.target_component,
                state=release.state,
                canary_percentage=release.canary_percentage,
                incumbent_sample_size=inc_total,
                candidate_sample_size=cand_total,
                incumbent_error_rate=round(inc_error_rate, 4),
                candidate_error_rate=round(cand_error_rate, 4),
                relative_risk=relative_risk,
                invariant_breach_count=total_invariant_breaches,
                recommendation=recommendation,
                last_breach_detail=audit_detail,
            )

    async def evaluate_and_enforce_guardrails(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        test_invariant_breaches: int = 0,
        invariant_breach_detail: str = "",
    ) -> tuple[bool, str]:
        """Runs guardrail check and executes automated rollback if any safety rule is violated.

        Returns (rolled_back: bool, reason: str).
        """
        metrics = await self.compute_canary_metrics(
            tenant_id=tenant_id,
            release_id=release_id,
            test_invariant_breaches=test_invariant_breaches,
        )

        release = await self._release_service.get_release(
            tenant_id=tenant_id, release_id=release_id
        )

        if release.state not in (ReleaseState.CANARY.value, ReleaseState.SHADOW.value, ReleaseState.ACTIVE.value):
            return (False, f"Release is in state {release.state}, guardrail evaluation skipped.")

        # 1. Class 1: Zero-Tolerance Invariant Breaches (N=1)
        if metrics.invariant_breach_count > 0:
            breach_msg = invariant_breach_detail or metrics.last_breach_detail or "Unauthorized mutation or negative-control violation"
            reason = f"CLASS_1_INVARIANT_BREACH: {breach_msg}"
            logger.error("Tripping Class 1 guardrail rollback on release %s: %s", release_id, reason)
            await self._release_service.trigger_rollback(
                tenant_id=tenant_id,
                release_id=release_id,
                expected_version=release.version,
                rollback_reason=reason,
                actor_id="guardrail-service",
                actor_role="system",
            )
            return (True, reason)

        # 2. Class 2: Statistical Rate Breaches (N >= 30, Err > 5%, RR > 1.5)
        if metrics.recommendation == "ROLLBACK_REQUIRED_STATISTICAL_RATE":
            reason = (
                f"CLASS_2_STATISTICAL_RATE_BREACH: candidate error rate {metrics.candidate_error_rate * 100:.1f}% "
                f"> 5.0% and relative risk {metrics.relative_risk} > 1.5 (N={metrics.candidate_sample_size})"
            )
            logger.error("Tripping Class 2 guardrail rollback on release %s: %s", release_id, reason)
            await self._release_service.trigger_rollback(
                tenant_id=tenant_id,
                release_id=release_id,
                expected_version=release.version,
                rollback_reason=reason,
                actor_id="guardrail-service",
                actor_role="system",
            )
            return (True, reason)

        return (False, f"Guardrails healthy (recommendation={metrics.recommendation})")
