
"""E6 immutable policy draft registry; all rollout exposure remains disabled."""

import hashlib
import secrets
from time import perf_counter
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.evaluation.smoke import sha
from backend.policy.constants import INCUMBENT_POLICY_ID
from backend.learning.failures import validate_change
from backend.models import (
    EvaluationManifest, ImprovementCandidate, Incident, OutcomeRevision, PolicyAssignment,
    PolicyBundle, PolicyRollout, PolicyShadowObservation,
)


class PolicyConflict(Exception):
    pass


async def register_bundle(
    session: AsyncSession, tenant_id: str, candidate_id: UUID,
    evaluation_id: UUID,
) -> PolicyBundle:
    async with session.begin():
        candidate = await session.scalar(
            select(ImprovementCandidate).where(
                ImprovementCandidate.id == candidate_id,
                ImprovementCandidate.tenant_id == tenant_id,
                ImprovementCandidate.state == "EVALUATION_READY",
            )
        )
        evaluation = await session.scalar(
            select(EvaluationManifest).where(
                EvaluationManifest.id == evaluation_id,
                EvaluationManifest.tenant_id == tenant_id,
                EvaluationManifest.candidate_id == candidate_id,
            )
        )
        if candidate is None or evaluation is None or evaluation.state != "SMOKE_PASS":
            raise PolicyConflict("matching E5 smoke report required")
        if candidate.version != evaluation.candidate_version:
            raise PolicyConflict("candidate version differs from frozen evaluation")
        validate_change(candidate.kind, candidate.change_spec)
        payload = {"kind": candidate.kind, "change": candidate.change_spec}
        if sha(payload) != evaluation.candidate_digest:
            raise PolicyConflict("candidate changed after evaluation")
        artifact_digest = hashlib.sha256(
            f"{candidate.id}:{candidate.version}:{evaluation.id}:{evaluation.fixture_digest}".encode()
        ).hexdigest()
        content = {
            "base_policy_id": candidate.base_policy_id,
            "policy_version": f"{candidate.base_policy_id}@cand-v{candidate.version}",
            "artifact_digest": artifact_digest,
            "candidate_id": str(candidate.id),
            "candidate_version": candidate.version,
            "kind": candidate.kind,
            "change_spec": candidate.change_spec,
            "tool_catalog": "simulator-readonly-v1",
            "model_id": "none",
        }
        digest = sha(content)
        existing = await session.scalar(
            select(PolicyBundle).where(
                PolicyBundle.tenant_id == tenant_id,
                PolicyBundle.content_digest == digest,
            )
        )
        if existing is not None:
            return existing
        bundle = PolicyBundle(
            id=uuid5(NAMESPACE_URL, f"opsblaze/policy/{tenant_id}/{digest}"),
            tenant_id=tenant_id, candidate_id=candidate_id,
            evaluation_manifest_id=evaluation_id,
            content_digest=digest, content=content, state="DRAFT",
        )
        session.add(bundle)
    return bundle


async def create_rollout(
    session: AsyncSession, tenant_id: str, bundle_id: UUID,
    cohort_percent: int,
) -> PolicyRollout:
    if not 1 <= cohort_percent <= 5:
        raise PolicyConflict("lab preview cohort must be 1–5 percent")
    async with session.begin():
        bundle = await session.scalar(
            select(PolicyBundle).where(
                PolicyBundle.id == bundle_id, PolicyBundle.tenant_id == tenant_id,
            )
        )
        if bundle is None or bundle.state != "DRAFT":
            raise PolicyConflict("draft bundle not found")
        rollout = PolicyRollout(
            id=uuid4(), tenant_id=tenant_id, bundle_id=bundle_id,
            cohort_percent=cohort_percent, salt=secrets.token_hex(12),
            state="DRAFT", version=1,
        )
        session.add(rollout)
    return rollout


def dry_run_assignment(rollout: PolicyRollout, tenant_id: str, incident_id: UUID) -> bool:
    # Preview only. This is never called by admission or the Temporal worker.
    if tenant_id != rollout.tenant_id:
        return False
    digest = hashlib.sha256(
        f"{tenant_id}/{incident_id}/{rollout.salt}".encode()
    ).digest()
    bucket = int.from_bytes(digest[:8], "big") % 100
    return bucket < rollout.cohort_percent


async def stop_rollout(
    session: AsyncSession, tenant_id: str, rollout_id: UUID,
    expected_version: int, action: str,
) -> PolicyRollout | None:
    if action not in {"PAUSED", "ROLLED_BACK"}:
        raise PolicyConflict("invalid rollout state transition")
    async with session.begin():
        rollout = await session.scalar(
            select(PolicyRollout)
            .where(
                PolicyRollout.id == rollout_id,
                PolicyRollout.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if rollout is None:
            return None
        if rollout.version != expected_version:
            raise PolicyConflict("rollout changed; refetch before retrying")
        if rollout.state not in {"DRAFT", "PAUSED"}:
            raise PolicyConflict("rollout cannot be changed from current state")
        rollout.state = action
        rollout.version += 1
    return rollout


async def record_shadow_observation(
    session: AsyncSession, tenant_id: str, rollout_id: UUID, incident_id: UUID,
) -> PolicyShadowObservation | None:
    """Rehearse cohort choice without installing candidate policy or any effect."""
    async with session.begin():
        rollout = await session.scalar(
            select(PolicyRollout).where(
                PolicyRollout.id == rollout_id,
                PolicyRollout.tenant_id == tenant_id,
            ).with_for_update()
        )
        if rollout is None:
            return None
        if rollout.state != "DRAFT":
            raise PolicyConflict("stopped rollout cannot record new shadow decisions")
        bundle = await session.scalar(
            select(PolicyBundle).where(
                PolicyBundle.id == rollout.bundle_id,
                PolicyBundle.tenant_id == tenant_id,
            )
        )
        if (
            bundle is None or bundle.state != "DRAFT"
            or sha(bundle.content) != bundle.content_digest
        ):
            raise PolicyConflict("candidate bundle digest or state changed")
        incident = await session.scalar(
            select(Incident.id).where(
                Incident.id == incident_id,
                Incident.tenant_id == tenant_id,
            )
        )
        if incident is None:
            return None
        assignment = await session.get(
            PolicyAssignment, (tenant_id, incident_id)
        )
        if (
            assignment is None
            or assignment.policy_id != INCUMBENT_POLICY_ID
            or assignment.bundle_id is not None
            or assignment.rollout_id is not None
        ):
            raise PolicyConflict("incident not pinned to the known-good policy")

        selected = dry_run_assignment(rollout, tenant_id, incident_id)
        shadow_id = uuid5(
            NAMESPACE_URL,
            f"opsblaze/shadow/{tenant_id}/{rollout_id}/{incident_id}",
        )
        existing = await session.get(PolicyShadowObservation, shadow_id)
        if existing is not None:
            if (
                existing.tenant_id != tenant_id
                or existing.bundle_digest != bundle.content_digest
                or existing.would_assign != selected
            ):
                raise PolicyConflict("recorded shadow decision cannot be rewritten")
            return existing
        shadow = PolicyShadowObservation(
            id=shadow_id, tenant_id=tenant_id, rollout_id=rollout_id,
            incident_id=incident_id, bundle_digest=bundle.content_digest,
            would_assign=selected,
        )
        session.add(shadow)
    return shadow


async def shadow_summary(
    session: AsyncSession, tenant_id: str, rollout_id: UUID,
) -> dict | None:
    rollout = await session.scalar(
        select(PolicyRollout).where(
            PolicyRollout.id == rollout_id, PolicyRollout.tenant_id == tenant_id,
        )
    )
    if rollout is None:
        return None
    total = await session.scalar(
        select(func.count()).select_from(PolicyShadowObservation).where(
            PolicyShadowObservation.tenant_id == tenant_id,
            PolicyShadowObservation.rollout_id == rollout_id,
        )
    )
    selected = await session.scalar(
        select(func.count()).select_from(PolicyShadowObservation).where(
            PolicyShadowObservation.tenant_id == tenant_id,
            PolicyShadowObservation.rollout_id == rollout_id,
            PolicyShadowObservation.would_assign.is_(True),
        )
    )
    return {
        "rollout_id": str(rollout_id), "state": rollout.state,
        "shadow_count": total or 0, "would_assign_count": selected or 0,
        "candidate_exposure": 0, "preview_only": True,
        "promotion_eligible": False,
    }


async def rehearse_simulator_canary(
    session: AsyncSession,
    tenant_id: str,
    rollout_id: UUID,
    expected_version: int,
    incident_ids: list[UUID],
    *,
    inject_unsafe: bool = False,
) -> dict | None:
    """E6-06/E6-07: bounded simulator canary rehearsal with unsafe abort & pin check."""
    if not incident_ids or len(incident_ids) > 25:
        raise PolicyConflict("canary rehearsal requires 1–25 incidents")
    t0 = perf_counter()
    async with session.begin():
        rollout = await session.scalar(
            select(PolicyRollout)
            .where(
                PolicyRollout.id == rollout_id,
                PolicyRollout.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if rollout is None:
            return None
        if rollout.version != expected_version:
            raise PolicyConflict("rollout version changed; refetch before retrying")
        if rollout.state != "DRAFT":
            raise PolicyConflict("only DRAFT rollouts may rehearse simulator canary")
        bundle = await session.scalar(
            select(PolicyBundle).where(
                PolicyBundle.id == rollout.bundle_id,
                PolicyBundle.tenant_id == tenant_id,
            )
        )
        if (
            bundle is None
            or bundle.state != "DRAFT"
            or sha(bundle.content) != bundle.content_digest
        ):
            raise PolicyConflict("candidate bundle digest or state invalid")

        pins_ok = True
        unsafe_detected = bool(inject_unsafe)
        would_assign_count = 0
        for iid in incident_ids:
            inc = await session.scalar(
                select(Incident.id).where(
                    Incident.id == iid, Incident.tenant_id == tenant_id,
                )
            )
            if inc is None:
                raise PolicyConflict("canary rehearsal incident not found in tenant")
            assignment = await session.get(PolicyAssignment, (tenant_id, iid))
            if (
                assignment is None
                or assignment.policy_id != INCUMBENT_POLICY_ID
                or assignment.bundle_id is not None
                or assignment.rollout_id is not None
            ):
                pins_ok = False
            if dry_run_assignment(rollout, tenant_id, iid):
                would_assign_count += 1
            latest_outcome = await session.scalar(
                select(OutcomeRevision.label)
                .where(
                    OutcomeRevision.tenant_id == tenant_id,
                    OutcomeRevision.incident_id == iid,
                )
                .order_by(OutcomeRevision.revision.desc())
                .limit(1)
            )
            if latest_outcome == "DISPUTED":
                unsafe_detected = True

        abort_reason: str | None = None
        if not pins_ok:
            unsafe_detected = True
            abort_reason = "ACTIVE_PIN_DRIFT_DETECTED"
        elif unsafe_detected:
            abort_reason = "UNSAFE_OUTCOME_OR_FAULT_DETECTED"

        if unsafe_detected:
            rollout.state = "ROLLED_BACK"
            rollout.version += 1

    elapsed_ms = (perf_counter() - t0) * 1000.0
    return {
        "rollout_id": str(rollout.id),
        "state": rollout.state,
        "version": rollout.version,
        "aborted": unsafe_detected,
        "abort_reason": abort_reason,
        "checked_incidents": len(incident_ids),
        "would_assign_count": would_assign_count,
        "active_pins_preserved": pins_ok,
        "added_admission_latency_ms": round(elapsed_ms, 3),
        "candidate_exposure": 0,
        "preview_only": True,
        "promotion_eligible": False,
    }
