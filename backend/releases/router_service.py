"""Feature E6 — Incident-Level Policy Routing, Deterministic Cohort Assignment & Active Pinning."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    CanaryArm,
    ReleaseState,
    assign_incident_cohort,
)
from backend.persistence.models import EvidenceLedger, Incident, Outbox, PolicyBundle, PolicyRelease, ReleaseEventAudit
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.shadow_executor import evaluate_bundle_shadow_policy

logger = logging.getLogger("opsblaze.releases.router_service")


class PolicyRouterService:
    """Manages active release routing, deterministic canary cohort assignment, and incident pinning."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        release_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._release_service = release_service

    async def route_incident(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_id: str,
        target_component: str = "investigator_planner",
    ) -> tuple[CanaryArm, uuid.UUID, uuid.UUID | None, int | None]:
        """Routes an incoming incident to INCUMBENT or CANDIDATE policy bundle.

        Returns (arm, pinned_policy_bundle_id, policy_release_id, hash_val).
        Pins the assignment into the Incident database row.
        """
        async with self._session_factory() as session:
            # 1. Fetch bootstrap/active incumbent bundle for this component
            stmt_inc = (
                select(PolicyBundle)
                .where(
                    PolicyBundle.tenant_id == tenant_id,
                    PolicyBundle.target_component == target_component,
                    PolicyBundle.bundle_name.ilike("%incumbent%"),
                )
                .order_by(PolicyBundle.created_at.desc())
            )
            incumbent_bundle = (await session.execute(stmt_inc)).scalars().first()
            if incumbent_bundle is None:
                # Fallback to any bundle for this component
                stmt_any = (
                    select(PolicyBundle)
                    .where(
                        PolicyBundle.tenant_id == tenant_id,
                        PolicyBundle.target_component == target_component,
                    )
                    .order_by(PolicyBundle.created_at.asc())
                )
                incumbent_bundle = (await session.execute(stmt_any)).scalars().first()
                if incumbent_bundle is None:
                    raise RuntimeError(f"No policy bundle found for component '{target_component}'.")

            # 2. Find active rollout release (CANARY, SHADOW, or ACTIVE)
            stmt_rel = (
                select(PolicyRelease)
                .where(
                    PolicyRelease.tenant_id == tenant_id,
                    PolicyRelease.target_component == target_component,
                    PolicyRelease.state.in_([ReleaseState.SHADOW.value, ReleaseState.CANARY.value, ReleaseState.ACTIVE.value]),
                )
                .order_by(PolicyRelease.updated_at.desc())
            )
            active_release = (await session.execute(stmt_rel)).scalars().first()

            arm, bundle_id, hash_val = assign_incident_cohort(
                tenant_id=tenant_id,
                incident_id=incident_id,
                target_id=target_id,
                release=active_release,
                default_incumbent_bundle_id=incumbent_bundle.id,
            )

            rel_id = active_release.id if active_release else None

            # 3. Pin onto Incident row
            incident = await session.get(Incident, incident_id)
            if incident is not None:
                incident.pinned_policy_bundle_id = bundle_id
                incident.policy_release_id = rel_id
                incident.canary_arm = arm.value
                incident.assignment_hash_val = hash_val
                await session.commit()

            return (arm, bundle_id, rel_id, hash_val)

    async def verify_consequential_mutation_allowed(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
    ) -> tuple[bool, str | None]:
        """Checks if consequential mutations are permitted for this incident.

        Fail-closed rules (Stage 11.1 Gate F):
        1. If incident does not exist under tenant -> block.
        2. If incident is on INCUMBENT arm or has no pinned release -> allow.
        3. If incident is on CANDIDATE arm:
           - Pinned policy release must exist -> if missing, block (fail closed).
           - Kill switch must NOT be active -> if active, block (fail closed).
           - Release state must be CANARY or ACTIVE -> if ROLLED_BACK, REVOKED, DRAFT,
             PENDING_APPROVAL, APPROVED, SHADOW, or unexpected -> block (fail closed).
        """
        async with self._session_factory() as session:
            incident = await session.get(Incident, incident_id)
            if incident is None or incident.tenant_id != tenant_id:
                return (False, f"Incident {incident_id} not found.")

            # If incident is on INCUMBENT arm or has no pinned release, allow
            if incident.canary_arm != CanaryArm.CANDIDATE.value or incident.policy_release_id is None:
                return (True, None)

            # Incident was assigned to CANDIDATE: inspect current release state
            release = await session.get(PolicyRelease, incident.policy_release_id)
            if release is None:
                return (False, f"Pinned policy release {incident.policy_release_id} not found (fail closed).")

            if release.kill_switch_active:
                return (
                    False,
                    f"Kill switch active on release {release.id} (triggered by {release.kill_switch_triggered_by}): consequential mutations blocked.",
                )

            try:
                current_state = ReleaseState(release.state)
            except ValueError:
                return (False, f"Release {release.id} has invalid state '{release.state}': consequential mutations blocked.")

            if current_state == ReleaseState.ROLLED_BACK:
                return (
                    False,
                    f"Policy release {release.id} was rolled back ({release.rollback_reason}): consequential mutations blocked.",
                )
            if current_state == ReleaseState.REVOKED:
                return (
                    False,
                    f"Policy release {release.id} was revoked: consequential mutations blocked.",
                )
            if current_state not in (ReleaseState.CANARY, ReleaseState.ACTIVE):
                return (
                    False,
                    f"Policy release {release.id} is in non-active state '{current_state.value}': consequential mutations blocked.",
                )

            return (True, None)

    async def execute_shadow_observation(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_id: str,
        release_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Run candidate and incumbent declarative policy rules over the same evidence snapshot.

        This path is read-only: it has no B2 reservation manager, P2 approval service,
        B3 action gateway, adapter, or A2 capability gateway.
        """
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                raise ValueError(f"Policy release {release_id} not found.")
            if release.state != ReleaseState.SHADOW.value or release.kill_switch_active:
                raise ValueError("Shadow evaluation requires a live SHADOW release with no kill switch.")

            incident = await session.get(Incident, incident_id)
            if incident is None or incident.tenant_id != tenant_id or incident.target_id != target_id:
                raise ValueError("Shadow incident or target is not valid for this tenant.")

            candidate_bundle = await session.get(PolicyBundle, release.bundle_id)
            incumbent_bundle = await session.get(PolicyBundle, release.incumbent_bundle_id)
            if candidate_bundle is None or incumbent_bundle is None:
                raise ValueError("Candidate or incumbent policy bundle is missing.")

            evidence = (
                await session.execute(
                    select(EvidenceLedger).where(
                        EvidenceLedger.tenant_id == tenant_id,
                        EvidenceLedger.incident_id == incident_id,
                        EvidenceLedger.target_id == target_id,
                        EvidenceLedger.validity_status == "VALID",
                    ).order_by(EvidenceLedger.observed_at.asc(), EvidenceLedger.id.asc())
                )
            ).scalars().all()

        bundle_service = PolicyBundleService(self._session_factory)
        await bundle_service.verify_bundle_integrity(tenant_id=tenant_id, bundle=candidate_bundle)
        await bundle_service.verify_bundle_integrity(tenant_id=tenant_id, bundle=incumbent_bundle)

        candidate_result = evaluate_bundle_shadow_policy(candidate_bundle, list(evidence))
        incumbent_result = evaluate_bundle_shadow_policy(incumbent_bundle, list(evidence))
        candidate_decision = candidate_result["decision"]
        incumbent_decision = incumbent_result["decision"]
        comparison = {
            "same_decision": candidate_decision == incumbent_decision,
            "candidate_only": candidate_decision != incumbent_decision,
            "evidence_count": len(evidence),
            "candidate_policy_status": candidate_result["status"],
            "incumbent_policy_status": incumbent_result["status"],
        }

        async with self._session_factory() as session:
            audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type="SHADOW_EVALUATION_COMPLETED",
                from_state=ReleaseState.SHADOW.value,
                to_state=ReleaseState.SHADOW.value,
                actor_id="shadow-runner",
                actor_role="system",
                metadata_redacted={
                    "incident_id": str(incident_id),
                    "target_id": target_id,
                    "candidate_bundle_id": str(candidate_bundle.id),
                    "candidate_bundle_sha256": candidate_bundle.bundle_sha256,
                    "incumbent_bundle_id": str(incumbent_bundle.id),
                    "incumbent_bundle_sha256": incumbent_bundle.bundle_sha256,
                    "candidate_result": candidate_result,
                    "incumbent_result": incumbent_result,
                    "comparison": comparison,
                    "execution_mode": "DECLARATIVE_READ_ONLY_SHADOW_V1",
                    "consequential_authority": False,
                    "tool_calls": 0,
                    "effect_emitted": False,
                },
                created_at=datetime.now(UTC),
            )
            session.add(audit)
            await session.commit()

        return {
            "incident_id": str(incident_id),
            "release_id": str(release_id),
            "candidate_bundle_id": str(candidate_bundle.id),
            "candidate_result": candidate_result,
            "incumbent_result": incumbent_result,
            "comparison": comparison,
            "consequential_authority": False,
            "tool_calls": 0,
            "effect_emitted": False,
            "shadow_status": (
                "EVALUATED"
                if candidate_result["status"] == "EVALUATED"
                else candidate_result["status"]
            ),
        }

    async def get_incident_pinned_policy(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Returns pinned policy details for an incident."""
        async with self._session_factory() as session:
            incident = await session.get(Incident, incident_id)
            if incident is None or incident.tenant_id != tenant_id:
                raise ValueError(f"Incident {incident_id} not found.")

            bundle = None
            if incident.pinned_policy_bundle_id is not None:
                bundle = await session.get(PolicyBundle, incident.pinned_policy_bundle_id)

            release = None
            if incident.policy_release_id is not None:
                release = await session.get(PolicyRelease, incident.policy_release_id)

            return {
                "incident_id": incident.id,
                "canary_arm": incident.canary_arm,
                "assignment_hash_val": incident.assignment_hash_val,
                "pinned_policy_bundle_id": incident.pinned_policy_bundle_id,
                "bundle_name": bundle.bundle_name if bundle else None,
                "bundle_sha256": bundle.bundle_sha256 if bundle else None,
                "policy_release_id": incident.policy_release_id,
                "release_state": release.state if release else None,
                "kill_switch_active": release.kill_switch_active if release else False,
            }
