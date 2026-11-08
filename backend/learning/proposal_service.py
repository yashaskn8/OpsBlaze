"""Service managing improvement proposals, validation, and review lifecycles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.failure import (
    FailureClusterStatus,
    ProposalConflictError,
    ProposalLifecycleError,
    ProposalNotFoundError,
    ProposalStatus,
    ProposalValidationError,
    ProposalValidator,
    ReviewDecision,
)
from backend.persistence.models import FailureCluster, ImprovementProposal


class ProposalService:
    """Manages the guarded lifecycle of model and human improvement proposals."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory

    async def create_draft(
        self,
        tenant_id: uuid.UUID,
        cluster_id: uuid.UUID,
        proposal_type: str,
        base_policy_version: str,
        target_component: str,
        structured_diff: dict[str, Any],
        hypothesis: str,
        expected_benefit: str,
        risk_assessment: str,
        evaluation_plan: str,
        drafter_id: str,
        drafter_kind: str = "MODEL",
    ) -> ImprovementProposal:
        """Creates a bounded improvement proposal draft with strict allowlist validation."""
        is_valid, rej_code, rej_reason = ProposalValidator.validate_proposal(
            proposal_type=proposal_type,
            target_component=target_component,
            structured_diff=structured_diff,
        )
        if not is_valid:
            raise ProposalValidationError(f"Invalid diff schema or component: {rej_reason}", code=rej_code)

        if self._session_factory:
            async with self._session_factory() as session:
                cluster_stmt = select(FailureCluster).where(
                    FailureCluster.tenant_id == tenant_id,
                    FailureCluster.id == cluster_id,
                )
                cluster = (await session.execute(cluster_stmt)).scalar_one_or_none()
                if cluster and cluster.cluster_status == FailureClusterStatus.QUARANTINED.value:
                    raise ProposalValidationError("Cluster is quarantined and cannot accept new proposals.")

        proposal = ImprovementProposal(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            cluster_id=cluster_id,
            proposal_type=proposal_type,
            base_policy_version=base_policy_version,
            target_component=target_component,
            structured_diff=structured_diff,
            hypothesis=hypothesis,
            expected_benefit=expected_benefit,
            risk_assessment=risk_assessment,
            evaluation_plan=evaluation_plan,
            status=ProposalStatus.DRAFT.value,
            version=1,
            drafter_kind=drafter_kind,
            drafter_id=drafter_id,
            rejection_code=None,
            rejection_reason=None,
        )

        if self._session_factory:
            async with self._session_factory() as session:
                session.add(proposal)
                await session.commit()
                await session.refresh(proposal)

        return proposal

    async def submit_proposal(
        self,
        tenant_id: uuid.UUID,
        proposal_id: uuid.UUID,
        expected_version: int,
    ) -> ImprovementProposal:
        """Transitions a draft proposal to PROPOSED for human review under CAS."""
        if not self._session_factory:
            raise RuntimeError("Database session factory required for proposal workflow.")

        async with self._session_factory() as session:
            stmt = select(ImprovementProposal).where(
                ImprovementProposal.tenant_id == tenant_id,
                ImprovementProposal.id == proposal_id,
            ).with_for_update()
            proposal = (await session.execute(stmt)).scalar_one_or_none()
            if not proposal:
                raise ProposalNotFoundError(f"Proposal '{proposal_id}' not found for tenant '{tenant_id}'.")

            if proposal.version != expected_version:
                raise ProposalConflictError(
                    f"CAS version conflict: version mismatch, expected {expected_version}, found {proposal.version}"
                )

            if proposal.status != ProposalStatus.DRAFT.value:
                raise ProposalLifecycleError(f"Cannot submit proposal in status '{proposal.status}'.")

            proposal.status = ProposalStatus.PROPOSED.value
            proposal.version += 1
            await session.commit()
            await session.refresh(proposal)
            return proposal

    async def review_proposal(
        self,
        tenant_id: uuid.UUID,
        proposal_id: uuid.UUID,
        expected_version: int,
        reviewer_id: str,
        reviewer_role: str,
        decision: str,
        comments: str | None = None,
    ) -> ImprovementProposal:
        """Records a human review decision under CAS optimistic concurrency."""
        if not self._session_factory:
            raise RuntimeError("Database session factory required for proposal review.")

        valid_decisions = {d.value for d in ReviewDecision}
        if decision not in valid_decisions:
            raise ValueError(f"Invalid review decision '{decision}'. Expected one of {valid_decisions}")

        async with self._session_factory() as session:
            stmt = select(ImprovementProposal).where(
                ImprovementProposal.tenant_id == tenant_id,
                ImprovementProposal.id == proposal_id,
            ).with_for_update()
            proposal = (await session.execute(stmt)).scalar_one_or_none()
            if not proposal:
                raise ProposalNotFoundError(f"Proposal '{proposal_id}' not found for tenant '{tenant_id}'.")

            if proposal.version != expected_version:
                raise ProposalConflictError(
                    f"CAS version conflict: version mismatch, expected {expected_version}, found {proposal.version}"
                )

            if decision == ReviewDecision.APPROVED.value:
                proposal.status = ProposalStatus.EVALUATION_READY.value
            elif decision == ReviewDecision.REJECTED.value:
                proposal.status = ProposalStatus.REJECTED.value
            elif decision == ReviewDecision.CHANGES_REQUESTED.value:
                proposal.status = ProposalStatus.DRAFT.value

            proposal.reviewer_id = reviewer_id
            proposal.reviewer_role = reviewer_role
            proposal.review_decision = decision
            proposal.review_comments = comments
            proposal.decided_at = datetime.now(UTC)
            proposal.version += 1

            await session.commit()
            await session.refresh(proposal)
            return proposal
