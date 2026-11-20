"""A1/A4 deterministic baseline. No LLM claims or calibrated probabilities."""

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import HypothesisStep
from backend.probes.gateway import ProbeDenied, invoke_probe


async def write_step(
    sessions: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    step_no: int,
    evidence_ids: list[str],
    decision: str,
    scores: dict[str, int],
    probe_id: str,
) -> None:
    async with sessions() as session:
        async with session.begin():
            row = await session.scalar(
                select(HypothesisStep).where(
                    HypothesisStep.incident_id == incident_id,
                    HypothesisStep.step_no == step_no,
                )
            )
            if row is not None:
                return
            session.add(
                HypothesisStep(
                    id=uuid4(), incident_id=incident_id, step_no=step_no,
                    model_id="deterministic-baseline-v1",
                    prompt_version="none",
                    evidence_ids=evidence_ids, cause_scores=scores,
                    probe_id=probe_id, decision=decision,
                    token_reserved=0, token_used=0,
                )
            )


async def inspect(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
) -> str:
    """Choose a second read based on the first observation. Never perform an effect."""
    try:
        health = await invoke_probe(
            sessions, tenant_id, incident_id, 1, "simulator.health"
        )
    except ProbeDenied:
        return "ABSTAINED"

    ids = [str(health.evidence_id)] if health.evidence_id else []
    if health.outcome != "OK":
        await write_step(sessions, incident_id, 1, ids, "ABSTAINED_NO_SOURCE",
                         {}, "simulator.health")
        return "ABSTAINED"

    current = json.loads(health.excerpt)
    if current.get("healthy", False):
        await write_step(sessions, incident_id, 1, ids, "ABSTAINED_NO_FAILURE",
                         {"no_simulated_fault": 1}, "simulator.health")
        return "ABSTAINED"

    await write_step(sessions, incident_id, 1, ids, "NEEDS_SECOND_PROBE",
                     {"simulated_target_unhealthy": 1}, "simulator.health")
    try:
        version = await invoke_probe(
            sessions, tenant_id, incident_id, 2, "simulator.version"
        )
    except ProbeDenied:
        return "ABSTAINED"
    if version.evidence_id:
        ids.append(str(version.evidence_id))
    await write_step(
        sessions, incident_id, 2, ids,
        "SIMULATOR_REVIEW_REQUIRED" if version.outcome == "OK" else "ABSTAINED",
        {"simulated_target_unhealthy": 1} if version.outcome == "OK" else {},
        "simulator.version",
    )
    return "SIMULATOR_REVIEW_REQUIRED" if version.outcome == "OK" else "ABSTAINED"
