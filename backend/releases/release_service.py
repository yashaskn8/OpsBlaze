"""Feature E6 — Governed Policy Release Service with Atomic CAS Transitions.

Responsible for:
- Managing the 8-state release lifecycle:
  DRAFT -> PENDING_APPROVAL -> APPROVED -> SHADOW -> CANARY -> ACTIVE -> ROLLED_BACK / REVOKED
- Enforcing strict separation of duties (approver != drafter).
- Enforcing authorized reviewer roles (release_owner, incident_commander).
- Re-checking underlying E5 candidate eligibility before release transitions.
- Ensuring single active release per (tenant_id, target_component) anti-split-brain invariant.
- Recording append-only immutable audit events for every state transition.
- Emergency Kill-Switch with instantaneous consequential traffic isolation.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    ACTIVE_RELEASE_STATES,
    ActiveReleaseConflictError,
    BundleNotFoundError,
    BundleNotSealedError,
    E5EligibilityInvalidatedError,
    InvalidReleaseTransitionError,
    ReleaseEventType,
    ReleaseNotFoundError,
    ReleaseSeparationOfDutiesError,
    ReleaseState,
    RolloutPercentageOutOfBoundsError,
    StaleVersionConflictError,
)
from backend.persistence.models import (
    CandidateEvaluationReport,
    PolicyBundle,
    PolicyRelease,
    ReleaseEventAudit,
)
from backend.releases.bundle_service import PolicyBundleService

logger = logging.getLogger("opsblaze.releases.release_service")


class PolicyReleaseService:
    """Manages policy release creation, approval, rollout transitions, audit logging, and rollback."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle_service: PolicyBundleService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._bundle_service = bundle_service or PolicyBundleService(session_factory)

    async def _assert_e5_eligibility(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        bundle_id: uuid.UUID,
    ) -> None:
        """Verifies that candidate bundle's underlying E5 evaluation report remains valid (Stage 11.1 Gate H)."""
        bundle = await session.get(PolicyBundle, bundle_id)
        if bundle is None:
            raise BundleNotFoundError(bundle_id)
        if bundle.source_report_id is not None:
            report = await session.get(CandidateEvaluationReport, bundle.source_report_id)
            if report is None or report.report_status != "PASS" or not report.promotion_eligible:
                raise E5EligibilityInvalidatedError(
                    f"Underlying report {bundle.source_report_id} status is '{report.report_status if report else 'MISSING'}'"
                )
            if report.superseded_by_report_id is not None:
                raise E5EligibilityInvalidatedError(
                    f"Underlying report was superseded by {report.superseded_by_report_id}"
                )

    async def create_release(
        self,
        *,
        tenant_id: uuid.UUID,
        target_component: str,
        bundle_id: uuid.UUID,
        incumbent_bundle_id: uuid.UUID,
        drafter_id: str,
        rollout_salt: str | None = None,
    ) -> PolicyRelease:
        """Create a new policy release in DRAFT state."""
        async with self._session_factory() as session:
            # 1. Validate Candidate Bundle & E5 eligibility
            candidate = await session.get(PolicyBundle, bundle_id)
            if candidate is None or candidate.tenant_id != tenant_id:
                raise BundleNotFoundError(bundle_id)
            if candidate.status != "SEALED":
                raise BundleNotSealedError(bundle_id, candidate.status)
            if candidate.target_component != target_component:
                raise ValueError(
                    f"Candidate bundle component '{candidate.target_component}' does not match release component '{target_component}'."
                )
            await self._assert_e5_eligibility(session, tenant_id, bundle_id)

            # 2. Validate Incumbent Bundle
            incumbent = await session.get(PolicyBundle, incumbent_bundle_id)
            if incumbent is None or incumbent.tenant_id != tenant_id:
                raise BundleNotFoundError(incumbent_bundle_id)
            if incumbent.status != "SEALED":
                raise BundleNotSealedError(incumbent_bundle_id, incumbent.status)
            if incumbent.target_component != target_component:
                raise ValueError(
                    f"Incumbent bundle component '{incumbent.target_component}' does not match release component '{target_component}'."
                )

            # 3. Create release record
            salt = rollout_salt or secrets.token_hex(16)
            release_id = uuid.uuid4()
            now = datetime.now(UTC)

            release = PolicyRelease(
                id=release_id,
                tenant_id=tenant_id,
                version=1,
                target_component=target_component,
                bundle_id=bundle_id,
                incumbent_bundle_id=incumbent_bundle_id,
                state=ReleaseState.DRAFT.value,
                canary_percentage=0,
                rollout_salt=salt,
                drafter_id=drafter_id,
                reviewer_id=None,
                reviewer_role=None,
                decision_comments=None,
                kill_switch_active=False,
                kill_switch_triggered_by=None,
                kill_switch_triggered_at=None,
                rollback_reason=None,
                created_at=now,
                updated_at=now,
                approved_at=None,
                activated_at=None,
                rolled_back_at=None,
            )
            session.add(release)

            # 4. Audit Log
            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.DRAFT_CREATED.value,
                from_state=None,
                to_state=ReleaseState.DRAFT.value,
                actor_id=drafter_id,
                actor_role="drafter",
                metadata_redacted={
                    "target_component": target_component,
                    "bundle_id": str(bundle_id),
                    "incumbent_bundle_id": str(incumbent_bundle_id),
                },
                created_at=now,
            )
            session.add(audit)

            await session.commit()
            await session.refresh(release)
            return release

    async def submit_for_approval(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic CAS transition: DRAFT -> PENDING_APPROVAL."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state != ReleaseState.DRAFT:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.PENDING_APPROVAL)

            await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state == ReleaseState.DRAFT.value,
                )
                .values(
                    state=ReleaseState.PENDING_APPROVAL.value,
                    version=expected_version + 1,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.SUBMITTED_FOR_APPROVAL.value,
                from_state=ReleaseState.DRAFT.value,
                to_state=ReleaseState.PENDING_APPROVAL.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def review_release(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        reviewer_id: str,
        reviewer_role: str,
        decision: str,  # 'APPROVED' or 'REJECTED'
        decision_comments: str | None = None,
    ) -> PolicyRelease:
        """Atomic CAS transition: PENDING_APPROVAL -> APPROVED or REVOKED."""
        allowed_roles = {"release_owner", "incident_commander"}
        if reviewer_role not in allowed_roles:
            raise PermissionError(f"Reviewer role '{reviewer_role}' not authorized to approve policy releases.")

        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state != ReleaseState.PENDING_APPROVAL:
                raise InvalidReleaseTransitionError(
                    current_state,
                    ReleaseState.APPROVED if decision == "APPROVED" else ReleaseState.REVOKED,
                )

            # Strict separation of duties: Approver != Drafter
            if reviewer_id == release.drafter_id:
                raise ReleaseSeparationOfDutiesError(
                    drafter_id=release.drafter_id, reviewer_id=reviewer_id
                )

            target_state = ReleaseState.APPROVED if decision == "APPROVED" else ReleaseState.REVOKED

            # If approving, re-check candidate E5 report eligibility (Stage 11.1 Gate H)
            if target_state == ReleaseState.APPROVED:
                await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state == ReleaseState.PENDING_APPROVAL.value,
                )
                .values(
                    state=target_state.value,
                    version=expected_version + 1,
                    reviewer_id=reviewer_id,
                    reviewer_role=reviewer_role,
                    decision_comments=decision_comments,
                    approved_at=now if target_state == ReleaseState.APPROVED else None,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit_type = (
                ReleaseEventType.APPROVAL_GRANTED.value
                if target_state == ReleaseState.APPROVED
                else ReleaseEventType.APPROVAL_REJECTED.value
            )
            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=audit_type,
                from_state=ReleaseState.PENDING_APPROVAL.value,
                to_state=target_state.value,
                actor_id=reviewer_id,
                actor_role=reviewer_role,
                metadata_redacted={"decision": decision, "comments": decision_comments},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def activate_shadow(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic CAS transition: APPROVED -> SHADOW."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state != ReleaseState.APPROVED:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.SHADOW)

            await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            # Check for existing active release on target component
            active = await self._find_active_release(session, tenant_id, release.target_component)
            if active is not None and active.id != release_id:
                raise ActiveReleaseConflictError(release.target_component, active.id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state == ReleaseState.APPROVED.value,
                )
                .values(
                    state=ReleaseState.SHADOW.value,
                    version=expected_version + 1,
                    canary_percentage=0,
                    activated_at=now,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.SHADOW_ACTIVATED.value,
                from_state=ReleaseState.APPROVED.value,
                to_state=ReleaseState.SHADOW.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def activate_canary(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        canary_percentage: int,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic CAS transition: APPROVED -> CANARY or SHADOW -> CANARY."""
        if not (1 <= canary_percentage <= 100):
            raise RolloutPercentageOutOfBoundsError(canary_percentage)

        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state not in (ReleaseState.APPROVED, ReleaseState.SHADOW):
                raise InvalidReleaseTransitionError(current_state, ReleaseState.CANARY)

            await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            # Check for existing active release on target component
            active = await self._find_active_release(session, tenant_id, release.target_component)
            if active is not None and active.id != release_id:
                raise ActiveReleaseConflictError(release.target_component, active.id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state.in_([ReleaseState.APPROVED.value, ReleaseState.SHADOW.value]),
                )
                .values(
                    state=ReleaseState.CANARY.value,
                    version=expected_version + 1,
                    canary_percentage=canary_percentage,
                    activated_at=release.activated_at or now,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.CANARY_ACTIVATED.value,
                from_state=current_state.value,
                to_state=ReleaseState.CANARY.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={"canary_percentage": canary_percentage},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def update_canary_percentage(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        new_percentage: int,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Updates canary percentage for an active CANARY release."""
        if not (0 <= new_percentage <= 100):
            raise RolloutPercentageOutOfBoundsError(new_percentage)

        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state != ReleaseState.CANARY:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.CANARY)

            await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state == ReleaseState.CANARY.value,
                )
                .values(
                    canary_percentage=new_percentage,
                    version=expected_version + 1,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.CANARY_PERCENTAGE_UPDATED.value,
                from_state=ReleaseState.CANARY.value,
                to_state=ReleaseState.CANARY.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={
                    "old_percentage": release.canary_percentage,
                    "new_percentage": new_percentage,
                },
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def promote_active(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic CAS transition: CANARY -> ACTIVE (100% traffic)."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state != ReleaseState.CANARY:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.ACTIVE)

            await self._assert_e5_eligibility(session, tenant_id, release.bundle_id)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state == ReleaseState.CANARY.value,
                )
                .values(
                    state=ReleaseState.ACTIVE.value,
                    version=expected_version + 1,
                    canary_percentage=100,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.ACTIVE_PROMOTED.value,
                from_state=ReleaseState.CANARY.value,
                to_state=ReleaseState.ACTIVE.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def trigger_rollback(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        rollback_reason: str,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic rollback from any active state (SHADOW, CANARY, ACTIVE) -> ROLLED_BACK."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            if current_state not in ACTIVE_RELEASE_STATES:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.ROLLED_BACK)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state.in_([s.value for s in ACTIVE_RELEASE_STATES]),
                )
                .values(
                    state=ReleaseState.ROLLED_BACK.value,
                    version=expected_version + 1,
                    canary_percentage=0,
                    rollback_reason=rollback_reason,
                    rolled_back_at=now,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.ROLLBACK_TRIGGERED.value,
                from_state=current_state.value,
                to_state=ReleaseState.ROLLED_BACK.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={"reason": rollback_reason},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def trip_kill_switch(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        justification: str,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Emergency Kill Switch: instantly terminates candidate traffic and marks release ROLLED_BACK."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                )
                .values(
                    kill_switch_active=True,
                    kill_switch_triggered_by=actor_id,
                    kill_switch_triggered_at=now,
                    state=ReleaseState.ROLLED_BACK.value,
                    rollback_reason=f"KILL_SWITCH: {justification}",
                    canary_percentage=0,
                    rolled_back_at=now,
                    version=expected_version + 1,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type=ReleaseEventType.KILL_SWITCH_TRIPPED.value,
                from_state=release.state,
                to_state=ReleaseState.ROLLED_BACK.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={"justification": justification},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def _find_active_release(
        self, session: AsyncSession, tenant_id: uuid.UUID, target_component: str
    ) -> PolicyRelease | None:
        stmt = (
            select(PolicyRelease)
            .where(
                PolicyRelease.tenant_id == tenant_id,
                PolicyRelease.target_component == target_component,
                PolicyRelease.state.in_([s.value for s in ACTIVE_RELEASE_STATES]),
            )
            .order_by(PolicyRelease.updated_at.desc())
        )
        res = await session.execute(stmt)
        return res.scalars().first()

    async def get_active_release(
        self, *, tenant_id: uuid.UUID, target_component: str
    ) -> PolicyRelease | None:
        async with self._session_factory() as session:
            return await self._find_active_release(session, tenant_id, target_component)

    async def revoke_release(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        expected_version: int,
        revocation_reason: str,
        actor_id: str,
        actor_role: str,
    ) -> PolicyRelease:
        """Atomic CAS transition: (DRAFT, PENDING_APPROVAL, APPROVED, SHADOW) -> REVOKED."""
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)

            if release.version != expected_version:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            current_state = ReleaseState(release.state)
            allowed_from = {ReleaseState.DRAFT, ReleaseState.PENDING_APPROVAL, ReleaseState.APPROVED, ReleaseState.SHADOW}
            if current_state not in allowed_from:
                raise InvalidReleaseTransitionError(current_state, ReleaseState.REVOKED)

            now = datetime.now(UTC)
            stmt = (
                update(PolicyRelease)
                .where(
                    PolicyRelease.id == release_id,
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.version == expected_version,
                    PolicyRelease.state.in_([s.value for s in allowed_from]),
                )
                .values(
                    state=ReleaseState.REVOKED.value,
                    version=expected_version + 1,
                    rollback_reason=revocation_reason,
                    updated_at=now,
                )
            )
            res = await session.execute(stmt)
            cursor = cast(CursorResult[Any], res)
            if cursor.rowcount == 0:
                raise StaleVersionConflictError(release_id, release.version, expected_version)

            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type="RELEASE_REVOKED",
                from_state=current_state.value,
                to_state=ReleaseState.REVOKED.value,
                actor_id=actor_id,
                actor_role=actor_role,
                metadata_redacted={"revocation_reason": revocation_reason},
                created_at=now,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(release)
            return release

    async def get_release(
        self, *, tenant_id: uuid.UUID, release_id: uuid.UUID
    ) -> PolicyRelease:
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ReleaseNotFoundError(release_id)
            return release

    async def list_releases(
        self,
        *,
        tenant_id: uuid.UUID,
        target_component: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[PolicyRelease]:
        async with self._session_factory() as session:
            stmt = select(PolicyRelease).where(PolicyRelease.tenant_id == tenant_id)
            if target_component:
                stmt = stmt.where(PolicyRelease.target_component == target_component)
            if state:
                stmt = stmt.where(PolicyRelease.state == state)
            stmt = stmt.order_by(PolicyRelease.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_release_audits(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        limit: int = 100,
    ) -> list[ReleaseEventAudit]:
        async with self._session_factory() as session:
            stmt = (
                select(ReleaseEventAudit)
                .where(
                    ReleaseEventAudit.tenant_id == tenant_id,
                    ReleaseEventAudit.release_id == release_id,
                )
                .order_by(ReleaseEventAudit.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
