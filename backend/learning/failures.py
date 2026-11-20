
"""E3 deterministic failure classification and safe, reviewable proposal queue.

No external model is trusted to classify root causes or modify permissions.
"""

from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    EffectReceipt, FailureCase, FailureCluster, ImprovementCandidate,
    Incident, OutcomeRevision,
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
        eligible = await session.scalar(
            select(FailureCase)
            .where(
                FailureCase.tenant_id == tenant_id,
                FailureCase.cluster_id == cluster_id,
                FailureCase.review_status == "CLASSIFIED",
            )
            .limit(1)
        )
        if eligible is None:
            raise FailureConflict("cluster has no classified source case")
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
        # Revalidate the exact allowlist at the evaluation transition.
        validate_change(candidate.kind, candidate.change_spec)
        candidate.state = "EVALUATION_READY"
        candidate.version += 1
    return candidate
