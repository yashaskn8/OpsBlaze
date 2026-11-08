"""Feature E6 — Incident Policy Router and Consequential Mutation Fencing Service.

Responsible for:
- Deterministic cohort assignment during incident admission.
- Atomically pinning policy bundle, release id, and arm to admitted incidents.
- Enforcing consequential action fencing when a release is rolled back or kill-switched.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    CanaryArm,
    ReleaseState,
    assign_incident_cohort,
)
from backend.persistence.models import Incident, PolicyBundle, PolicyRelease
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.release_service import PolicyReleaseService

logger = logging.getLogger("opsblaze.releases.router_service")


class PolicyRouterService:
    """Routes incidents to policy bundles and fences consequential mutations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        release_service: PolicyReleaseService | None = None,
        bundle_service: PolicyBundleService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._release_service = release_service or PolicyReleaseService(session_factory)
        self._bundle_service = bundle_service or PolicyBundleService(session_factory)

    async def route_incident(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_id: str,
        target_component: str = "investigator",
    ) -> tuple[CanaryArm, uuid.UUID, uuid.UUID | None, int | None]:
        """Resolves active release and assigns incident to cohort.

        Returns (arm, pinned_bundle_id, policy_release_id, assignment_hash_val).
        """
        # Ensure a baseline incumbent exists
        incumbent = await self._bundle_service.ensure_bootstrap_incumbent(
            tenant_id=tenant_id,
            target_component=target_component,
        )

        active_release = await self._release_service.get_active_release(
            tenant_id=tenant_id,
            target_component=target_component,
        )

        arm, bundle_id, hash_val = assign_incident_cohort(
            tenant_id=tenant_id,
            incident_id=incident_id,
            target_id=target_id,
            release=active_release,
            default_incumbent_bundle_id=incumbent.id,
        )

        release_id = active_release.id if active_release is not None else None
        return (arm, bundle_id, release_id, hash_val)

    async def verify_consequential_mutation_allowed(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
    ) -> tuple[bool, str | None]:
        """Checks if consequential mutations are permitted for this incident.

        If the incident is running on a candidate release that has since been rolled back
        or has its kill-switch active, consequential mutations (B2 reservations, P2 dispatches)
        are blocked server-side, requiring human escalation.
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
                return (False, f"Pinned policy release {incident.policy_release_id} not found.")

            if release.kill_switch_active:
                return (
                    False,
                    f"Kill switch active on release {release.id} (triggered by {release.kill_switch_triggered_by}): consequential mutations blocked.",
                )

            current_state = ReleaseState(release.state)
            if current_state == ReleaseState.ROLLED_BACK:
                return (
                    False,
                    f"Policy release {release.id} was rolled back ({release.rollback_reason}): consequential mutations blocked.",
                )

            return (True, None)

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
