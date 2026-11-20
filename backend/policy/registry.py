
"""E6 immutable policy draft registry; all rollout exposure remains disabled."""

import hashlib
import json
import secrets
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.evaluation.smoke import sha
from backend.learning.failures import validate_change
from backend.models import (
    EvaluationManifest, ImprovementCandidate, PolicyBundle, PolicyRollout,
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
        content = {
            "base_policy_id": candidate.base_policy_id,
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
