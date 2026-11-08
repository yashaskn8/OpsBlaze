"""Feature E6 — Release Safety Guardrails and Automated Rollback Service.

Evaluates active CANARY and SHADOW releases against two tiers of safety guardrails:
1. Class 1 (Zero-Tolerance Invariants):
   - N = 1 trigger on unauthorized mutations, negative-control breaches, or B2 fence violations.
   - Triggers instantaneous automated rollback.
2. Class 2 (Statistical Rate Metrics):
   - Evaluated after minimum sample size N_min >= 30.
   - Triggers automated rollback if Candidate Error Rate > 5% and Relative Risk (RR) vs concurrent Incumbent > 1.5.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    CanaryArm,
    CanaryMetricsResponse,
    ReleaseState,
)
from backend.persistence.models import Incident, PolicyRelease
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

    async def compute_canary_metrics(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        simulated_invariant_breaches: int = 0,
    ) -> CanaryMetricsResponse:
        """Calculates error rates, sample sizes, and relative risk for active canary."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ValueError(f"Release {release_id} not found.")

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
            if simulated_invariant_breaches > 0:
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
                invariant_breach_count=simulated_invariant_breaches,
                recommendation=recommendation,
            )

    async def evaluate_and_enforce_guardrails(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        simulated_invariant_breaches: int = 0,
        invariant_breach_detail: str = "",
    ) -> tuple[bool, str]:
        """Runs guardrail check and executes automated rollback if any safety rule is violated.

        Returns (rolled_back: bool, reason: str).
        """
        metrics = await self.compute_canary_metrics(
            tenant_id=tenant_id,
            release_id=release_id,
            simulated_invariant_breaches=simulated_invariant_breaches,
        )

        release = await self._release_service.get_release(
            tenant_id=tenant_id, release_id=release_id
        )

        if release.state not in (ReleaseState.CANARY.value, ReleaseState.SHADOW.value, ReleaseState.ACTIVE.value):
            return (False, f"Release is in state {release.state}, guardrail evaluation skipped.")

        # 1. Class 1: Zero-Tolerance Invariant Breaches (N=1)
        if metrics.invariant_breach_count > 0:
            reason = f"CLASS_1_INVARIANT_BREACH: {invariant_breach_detail or 'Unauthorized mutation or negative-control violation'}"
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
