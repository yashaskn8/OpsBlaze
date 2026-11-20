
"""Fail-closed invalidation of learning artifacts after outcome reversal.

Called within the E2 outcome revision transaction; no external I/O.
Existing audit records are retained, but invalid sources cannot be retrieved,
evaluated, or rolled out. Only source-linked tenant records are touched.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    EvaluationManifest, ExperienceCapsule, FailureCase, FailureCluster,
    ImprovementCandidate, OperationalLesson, PolicyBundle, PolicyRollout,
)


async def invalidate_outcome_dependents(
    session: AsyncSession, tenant_id: str, incident_id: UUID,
) -> None:
    capsule_ids = list((await session.scalars(
        select(ExperienceCapsule.id).where(
            ExperienceCapsule.tenant_id == tenant_id,
            ExperienceCapsule.incident_id == incident_id,
        )
    )).all())
    if capsule_ids:
        await session.execute(
            update(ExperienceCapsule)
            .where(
                ExperienceCapsule.tenant_id == tenant_id,
                ExperienceCapsule.id.in_(capsule_ids),
                ExperienceCapsule.status == "VALID",
            )
            .values(status="QUARANTINED")
        )
        await session.execute(
            update(OperationalLesson)
            .where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.source_capsule_id.in_(capsule_ids),
                OperationalLesson.status != "REVOKED",
            )
            .values(
                status="REVOKED",
                version=OperationalLesson.version + 1,
            )
        )

    cluster_ids = list((await session.scalars(
        select(FailureCase.cluster_id).where(
            FailureCase.tenant_id == tenant_id,
            FailureCase.incident_id == incident_id,
        )
    )).all())
    if not cluster_ids:
        return

    await session.execute(
        update(FailureCase)
        .where(
            FailureCase.tenant_id == tenant_id,
            FailureCase.incident_id == incident_id,
        )
        .values(review_status="STALE")
    )
    await session.execute(
        update(FailureCluster)
        .where(
            FailureCluster.tenant_id == tenant_id,
            FailureCluster.id.in_(cluster_ids),
        )
        .values(quarantined=True)
    )

    candidate_ids = list((await session.scalars(
        select(ImprovementCandidate.id).where(
            ImprovementCandidate.tenant_id == tenant_id,
            ImprovementCandidate.source_cluster_id.in_(cluster_ids),
        )
    )).all())
    if not candidate_ids:
        return

    await session.execute(
        update(ImprovementCandidate)
        .where(
            ImprovementCandidate.tenant_id == tenant_id,
            ImprovementCandidate.id.in_(candidate_ids),
        )
        .values(state="INVALID", version=ImprovementCandidate.version + 1)
    )
    eval_ids = list((await session.scalars(
        select(EvaluationManifest.id).where(
            EvaluationManifest.tenant_id == tenant_id,
            EvaluationManifest.candidate_id.in_(candidate_ids),
        )
    )).all())
    if not eval_ids:
        return

    await session.execute(
        update(EvaluationManifest)
        .where(
            EvaluationManifest.tenant_id == tenant_id,
            EvaluationManifest.id.in_(eval_ids),
        )
        .values(state="INVALID")
    )
    bundle_ids = list((await session.scalars(
        select(PolicyBundle.id).where(
            PolicyBundle.tenant_id == tenant_id,
            PolicyBundle.evaluation_manifest_id.in_(eval_ids),
        )
    )).all())
    if not bundle_ids:
        return

    await session.execute(
        update(PolicyBundle)
        .where(
            PolicyBundle.tenant_id == tenant_id,
            PolicyBundle.id.in_(bundle_ids),
        )
        .values(state="REVOKED")
    )
    await session.execute(
        update(PolicyRollout)
        .where(
            PolicyRollout.tenant_id == tenant_id,
            PolicyRollout.bundle_id.in_(bundle_ids),
            PolicyRollout.state != "ROLLED_BACK",
        )
        .values(
            state="ROLLED_BACK",
            version=PolicyRollout.version + 1,
        )
    )
