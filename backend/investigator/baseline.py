"""A1/A4 deterministic baseline. No LLM claims or calibrated probabilities."""

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import HypothesisStep, Incident
from backend.lineage_projection import replace_evidence_edges
from backend.probes.gateway import ProbeDenied, invoke_probe
from backend.plans import PlanConflict, propose_simulator_plan
from backend.investigator.advisory import advise_after_unhealthy
from backend.investigator.abstain import assess_decision


async def write_step(
    sessions: async_sessionmaker[AsyncSession],
    incident_id: UUID,
    step_no: int,
    evidence_ids: list[str],
    decision: str,
    scores: dict[str, int],
    probe_id: str,
    model_id: str = "deterministic-baseline-v1",
    token_used: int = 0,
    token_reserved: int = 0,
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
            tenant_id = await session.scalar(select(Incident.tenant_id).where(Incident.id == incident_id))
            if tenant_id is None:
                raise ValueError("incident not found")
            source = HypothesisStep(
                id=uuid4(), incident_id=incident_id, step_no=step_no,
                model_id=model_id, prompt_version="none",
                evidence_ids=evidence_ids, cause_scores=scores,
                probe_id=probe_id, decision=decision,
                token_reserved=token_reserved, token_used=token_used,
            )
            session.add(source)
            await session.flush()
            await replace_evidence_edges(session, tenant_id, incident_id, "hypothesis", source)


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
        await assess_decision(sessions, tenant_id, incident_id, 1, [], probe_failure="TOOL_BLOCKED")
        return "ABSTAINED"

    ids = [str(health.evidence_id)] if health.evidence_id else []
    if health.outcome != "OK":
        await assess_decision(
            sessions, tenant_id, incident_id, 1, ids,
            probe_failure="TOOL_BLOCKED" if health.outcome == "DENIED" else "TOOL_UNAVAILABLE",
        )
        await write_step(sessions, incident_id, 1, ids, "ABSTAINED_NO_SOURCE",
                         {}, "simulator.health")
        return "ABSTAINED"

    current = json.loads(health.excerpt)
    if current.get("healthy", False):
        await write_step(sessions, incident_id, 1, ids, "ABSTAINED_NO_FAILURE",
                         {"no_simulated_fault": 1}, "simulator.health")
        return "ABSTAINED"

    advice = await advise_after_unhealthy(sessions, tenant_id, incident_id)
    model_id = advice.model_id if advice else "deterministic-baseline-v1"
    token_used = advice.tokens_used if advice else 0
    token_reserved = advice.tokens_reserved if advice else 0
    if advice is not None and advice.next_probe == "ABSTAIN":
        await write_step(
            sessions, incident_id, 1, ids, "MODEL_ADVISORY_ABSTAINED",
            {"simulated_target_unhealthy": 1}, "simulator.health",
            model_id=model_id, token_used=token_used,
            token_reserved=token_reserved,
        )
        return "ABSTAINED"

    await write_step(
        sessions, incident_id, 1, ids, "NEEDS_SECOND_PROBE",
        {"simulated_target_unhealthy": 1}, "simulator.health",
        model_id=model_id, token_used=token_used,
            token_reserved=token_reserved,
    )
    try:
        version = await invoke_probe(
            sessions, tenant_id, incident_id, 2, "simulator.version"
        )
    except ProbeDenied:
        await assess_decision(sessions, tenant_id, incident_id, 2, ids, probe_failure="TOOL_BLOCKED")
        return "ABSTAINED"
    if version.evidence_id:
        ids.append(str(version.evidence_id))
    assessment = await assess_decision(
        sessions, tenant_id, incident_id, 2, ids,
        probe_failure=(
            None if version.outcome == "OK" else
            "TOOL_BLOCKED" if version.outcome == "DENIED" else "TOOL_UNAVAILABLE"
        ),
    )
    permitted = (
        version.outcome == "OK"
        and assessment is not None
        and assessment.outcome == "PROPOSED"
    )
    await write_step(
        sessions, incident_id, 2, ids,
        "SIMULATOR_REVIEW_REQUIRED" if permitted else "ABSTAINED",
        {"simulated_target_unhealthy": 1} if permitted else {},
        "simulator.version",
    )

    if not permitted:
        return "ABSTAINED"
    try:
        await propose_simulator_plan(
            sessions, tenant_id, incident_id,
            json.loads(version.excerpt)["version"], ids,
        )
    except (PlanConflict, KeyError, TypeError):
        return "ABSTAINED"
    return "SIMULATOR_REVIEW_REQUIRED"
