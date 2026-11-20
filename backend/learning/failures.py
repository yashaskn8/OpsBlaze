
"""E3 deterministic failure classification and safe, reviewable proposal queue.

No external model is trusted to classify root causes or modify permissions.
"""

from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    EffectReceipt, EvaluationManifest, FailureCase, FailureCluster,
    FailureReview, ImprovementCandidate, Incident, OutcomeRevision, ProbeCall,
    PolicyBundle, PolicyRollout,
)


class FailureConflict(Exception):
    pass


def classify(label: str, effect_state: str | None) -> tuple[str, str]:
    if label == "VERIFIED":
        return "NONE", "NOT_A_FAILURE"
    if label == "DISPUTED":
        return "OUTCOME_DISPUTED", "UNCERTAIN"
    if effect_state == "UNKNOWN":
        return "EFFECT_UNKNOWN", "CLASSIFIED"
    if effect_state == "FAILED":
        return "EFFECT_FAILED", "CLASSIFIED"
    return "UNCLASSIFIED", "UNCERTAIN"


async def record_case(
    session: AsyncSession, tenant_id: str, outcome_id: UUID,
) -> FailureCase | None:
    async with session.begin():
        outcome = await session.scalar(
            select(OutcomeRevision).where(
                OutcomeRevision.id == outcome_id,
                OutcomeRevision.tenant_id == tenant_id,
            )
        )
        if outcome is None:
            return None
        # E2 disputes lock this same parent before appending or invalidating.
        # Classification must not create a trusted case after invalidation.
        current_incident = await session.scalar(
            select(Incident.id)
            .where(
                Incident.id == outcome.incident_id,
                Incident.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if current_incident is None:
            raise FailureConflict("incident missing")
        newest = await session.scalar(
            select(OutcomeRevision.id)
            .where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == outcome.incident_id,
            )
            .order_by(OutcomeRevision.revision.desc())
            .limit(1)
        )
        if newest != outcome.id:
            raise FailureConflict("outcome source revision was superseded")
        existing = await session.scalar(
            select(FailureCase).where(
                FailureCase.tenant_id == tenant_id,
                FailureCase.outcome_revision_id == outcome_id,
            )
        )
        if existing is not None:
            return existing
        effect = None
        if outcome.effect_id:
            effect = await session.scalar(
                select(EffectReceipt).where(
                    EffectReceipt.id == outcome.effect_id,
                    EffectReceipt.tenant_id == tenant_id,
                    EffectReceipt.incident_id == outcome.incident_id,
                )
            )
        code, review = classify(
            outcome.label, effect.state if effect else None
        )
        if code == "NONE":
            raise FailureConflict("verified outcomes are not failure cases")
        signature = f"{code}:effect-{effect.state if effect else 'none'}"
        cluster_id = uuid5(NAMESPACE_URL, f"opsblaze/failure/{tenant_id}/{signature}/v1")
        cluster = await session.get(FailureCluster, cluster_id, with_for_update=True)
        if cluster is None:
            cluster = FailureCluster(
                id=cluster_id, tenant_id=tenant_id, signature=signature,
                version=1, case_count=0, quarantined=False,
            )
            session.add(cluster)
        cluster.case_count += 1
        case = FailureCase(
            id=uuid4(), tenant_id=tenant_id,
            incident_id=outcome.incident_id, outcome_revision_id=outcome.id,
            cluster_id=cluster_id, origin_code=code, first_bad_step=None,
            classification_version=1, review_status=review,
        )
        session.add(case)
    return case


def validate_change(kind: str, spec: dict) -> None:
    # Deliberately no extensible code, SQL, URL, or tool-permission fields.
    if not isinstance(spec, dict):
        raise FailureConflict("change must be an object")
    valid = {
        "PROBE_ORDER": lambda: (
            set(spec) == {"order"}
            and isinstance(spec["order"], list)
            and 1 <= len(spec["order"]) <= 2
            and all(isinstance(item, str) for item in spec["order"])
            and len(spec["order"]) == len(set(spec["order"]))
            and all(
                item in {"simulator.health", "simulator.version"}
                for item in spec["order"]
            )
        ),
        "CONTEXT_LIMIT": lambda: (
            set(spec) == {"max_context_tokens"}
            and type(spec["max_context_tokens"]) is int
            and 64 <= spec["max_context_tokens"] <= 512
        ),
        "ROUTE_SWITCH": lambda: (
            set(spec) == {"strategy"}
            and spec["strategy"] in {"deterministic", "no_memory"}
        ),
    }
    if kind not in valid or not valid[kind]():
        raise FailureConflict("change class or content is not allowlisted")


async def has_current_classified_case(
    session: AsyncSession, tenant_id: str, cluster_id: UUID,
) -> bool:
    """A signature alone is not enough; its case must cite the newest outcome."""
    newest_outcome = (
        select(OutcomeRevision.id)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == FailureCase.incident_id,
        )
        .order_by(OutcomeRevision.revision.desc())
        .limit(1)
        .correlate(FailureCase)
        .scalar_subquery()
    )
    matching = await session.scalar(
        select(FailureCase.id)
        .where(
            FailureCase.tenant_id == tenant_id,
            FailureCase.cluster_id == cluster_id,
            FailureCase.review_status == "CLASSIFIED",
            FailureCase.outcome_revision_id == newest_outcome,
        )
        .limit(1)
    )
    return matching is not None


async def draft_candidate(
    session: AsyncSession, tenant_id: str, cluster_id: UUID,
    kind: str, base_policy_id: str, change_spec: dict,
    hypothesis: str, risk: str, manifest: str,
) -> ImprovementCandidate:
    validate_change(kind, change_spec)
    if risk not in {"LOW", "MEDIUM", "HIGH"}:
        raise FailureConflict("invalid risk band")
    if not 8 <= len(hypothesis.strip()) <= 512:
        raise FailureConflict("hypothesis must be 8–512 characters")
    if not manifest or len(manifest) > 64 or not base_policy_id or len(base_policy_id) > 64:
        raise FailureConflict("base policy and evaluation manifest are required")

    async with session.begin():
        cluster = await session.scalar(
            select(FailureCluster).where(
                FailureCluster.id == cluster_id,
                FailureCluster.tenant_id == tenant_id,
            ).with_for_update()
        )
        if cluster is None or cluster.quarantined or cluster.case_count < 1:
            raise FailureConflict("cluster not available")
        if not await has_current_classified_case(session, tenant_id, cluster_id):
            raise FailureConflict("cluster has no current classified source case")
        candidate = ImprovementCandidate(
            id=uuid4(), tenant_id=tenant_id, source_cluster_id=cluster_id,
            kind=kind, base_policy_id=base_policy_id, change_spec=change_spec,
            hypothesis=hypothesis.strip(), risk=risk,
            evaluation_manifest_id=manifest,
            state="DRAFT", version=1, created_by="local_operator",
        )
        session.add(candidate)
    return candidate


async def submit_candidate(
    session: AsyncSession, tenant_id: str, candidate_id: UUID,
    expected_version: int,
) -> ImprovementCandidate | None:
    async with session.begin():
        candidate = await session.scalar(
            select(ImprovementCandidate)
            .where(
                ImprovementCandidate.id == candidate_id,
                ImprovementCandidate.tenant_id == tenant_id,
            ).with_for_update()
        )
        if candidate is None:
            return None
        if candidate.state != "DRAFT" or candidate.version != expected_version:
            raise FailureConflict("stale candidate version or state")
        cluster = await session.get(FailureCluster, candidate.source_cluster_id)
        if cluster is None or cluster.quarantined:
            raise FailureConflict("source cluster quarantined")
        if not await has_current_classified_case(
            session, tenant_id, candidate.source_cluster_id,
        ):
            raise FailureConflict("candidate source outcome was superseded")
        # Revalidate the exact allowlist at the evaluation transition.
        validate_change(candidate.kind, candidate.change_spec)
        candidate.state = "EVALUATION_READY"
        candidate.version += 1
    return candidate


async def review_classification(
    session: AsyncSession,
    tenant_id: str, case_id: UUID, expected_version: int,
    corrected_code: str, first_bad_step: int | None,
    reason: str,
) -> FailureCase | None:
    """Fact-bound correction. Invalidate candidates derived from old signatures."""
    if not 8 <= len(reason.strip()) <= 256:
        raise FailureConflict("review reason must contain 8–256 characters")
    if corrected_code not in {"PROBE_DENIED", "EFFECT_FAILED", "EFFECT_UNKNOWN"}:
        raise FailureConflict("unsupported correction code")
    async with session.begin():
        case = await session.scalar(
            select(FailureCase)
            .where(FailureCase.id == case_id, FailureCase.tenant_id == tenant_id)
            .with_for_update()
        )
        if case is None:
            return None
        if case.classification_version != expected_version:
            raise FailureConflict("classification version changed")
        latest_outcome = await session.scalar(
            select(OutcomeRevision)
            .where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == case.incident_id,
            )
            .order_by(OutcomeRevision.revision.desc())
            .limit(1)
        )
        if (
            latest_outcome is None
            or latest_outcome.id != case.outcome_revision_id
            or latest_outcome.label == "VERIFIED"
        ):
            raise FailureConflict("source outcome is stale or verified")

        if corrected_code == "PROBE_DENIED":
            if first_bad_step is None or first_bad_step < 1:
                raise FailureConflict("denied probe step is required")
            matching = await session.scalar(
                select(ProbeCall).where(
                    ProbeCall.incident_id == case.incident_id,
                    ProbeCall.step_no == first_bad_step,
                    ProbeCall.outcome == "DENIED",
                )
            )
        else:
            if first_bad_step is not None:
                raise FailureConflict("effect correction cannot name a probe step")
            matching = await session.scalar(
                select(EffectReceipt).where(
                    EffectReceipt.tenant_id == tenant_id,
                    EffectReceipt.incident_id == case.incident_id,
                    EffectReceipt.state == (
                        "FAILED" if corrected_code == "EFFECT_FAILED" else "UNKNOWN"
                    ),
                ).limit(1)
            )
        if matching is None:
            raise FailureConflict("no durable facts support this correction")

        old_cluster = await session.get(
            FailureCluster, case.cluster_id, with_for_update=True
        )
        if old_cluster is None:
            raise FailureConflict("source cluster missing")
        previous_code = case.origin_code
        signature = (
            f"{corrected_code}:step-{first_bad_step}"
            if first_bad_step is not None else f"{corrected_code}:effect"
        )
        new_cluster_id = uuid5(
            NAMESPACE_URL, f"opsblaze/failure/{tenant_id}/{signature}/v1"
        )
        new_cluster = await session.get(
            FailureCluster, new_cluster_id, with_for_update=True
        )
        if new_cluster is None:
            new_cluster = FailureCluster(
                id=new_cluster_id, tenant_id=tenant_id,
                signature=signature, version=1,
                case_count=0, quarantined=False,
            )
            session.add(new_cluster)
        if old_cluster.id != new_cluster.id:
            old_cluster.case_count -= 1
            old_cluster.quarantined = True
            new_cluster.case_count += 1
            await session.execute(
                update(ImprovementCandidate)
                .where(ImprovementCandidate.source_cluster_id == old_cluster.id)
                .values(state="INVALID")
            )
            candidates = (
                await session.scalars(
                    select(ImprovementCandidate.id).where(
                        ImprovementCandidate.source_cluster_id == old_cluster.id
                    )
                )
            ).all()
            if candidates:
                evaluation_ids = list((await session.scalars(
                    select(EvaluationManifest.id).where(
                        EvaluationManifest.tenant_id == tenant_id,
                        EvaluationManifest.candidate_id.in_(candidates),
                    )
                )).all())
                await session.execute(
                    update(EvaluationManifest)
                    .where(
                        EvaluationManifest.tenant_id == tenant_id,
                        EvaluationManifest.candidate_id.in_(candidates),
                    )
                    .values(state="INVALID")
                )
                if evaluation_ids:
                    bundle_ids = list((await session.scalars(
                        select(PolicyBundle.id).where(
                            PolicyBundle.tenant_id == tenant_id,
                            PolicyBundle.evaluation_manifest_id.in_(evaluation_ids),
                        )
                    )).all())
                    if bundle_ids:
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
        case.origin_code = corrected_code
        case.first_bad_step = first_bad_step
        case.review_status = "CLASSIFIED"
        case.cluster_id = new_cluster_id
        case.classification_version += 1
        session.add(
            FailureReview(
                id=uuid4(), tenant_id=tenant_id, case_id=case.id,
                revision=case.classification_version,
                previous_code=previous_code, corrected_code=corrected_code,
                reason=reason.strip(),
            )
        )
    return case
