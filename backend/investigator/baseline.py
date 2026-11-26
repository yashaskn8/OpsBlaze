"""A1/A3/A4 adaptive simulator investigation with competing hypotheses and immutable manifests."""

import hashlib
import json
import time
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import ContextManifest, HypothesisStep, Incident
from backend.lineage_projection import replace_evidence_edges
from backend.probes.gateway import ProbeDenied, invoke_probe
from backend.plan_proposals import PlanConflict, propose_simulator_plan
from backend.investigator.advisory import advise_after_unhealthy
from backend.investigator.abstain import assess_decision

RUNBOOK_REVISION = "simulator-runbook-v1"
PROMPT_REVISION_NONE = "none"
PROMPT_REVISION_ADVISORY = "advisory-readonly-v1"
DEFAULT_MAX_INVESTIGATION_SECONDS = 30.0


def build_competing_hypotheses(
    *,
    health_evidence_id: str | None = None,
    healthy: bool | None = None,
    version_evidence_id: str | None = None,
    permitted: bool | None = None,
) -> list[dict]:
    """A1-01: bounded competing hypotheses with supporting and opposing evidence citations."""
    if health_evidence_id is None:
        return [
            {
                "cause": "source_unavailable",
                "status": "SUPPORTED",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [],
                "score": 100,
            },
            {
                "cause": "simulated_target_unhealthy",
                "status": "CANDIDATE",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [],
                "score": 0,
            },
        ]
    if healthy is True:
        return [
            {
                "cause": "no_simulated_fault",
                "status": "SUPPORTED",
                "supporting_evidence_ids": [health_evidence_id],
                "counter_evidence_ids": [],
                "score": 100,
            },
            {
                "cause": "simulated_target_unhealthy",
                "status": "REFUTED",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [health_evidence_id],
                "score": 0,
            },
        ]
    if version_evidence_id is None:
        return [
            {
                "cause": "simulated_target_unhealthy",
                "status": "SUPPORTED",
                "supporting_evidence_ids": [health_evidence_id],
                "counter_evidence_ids": [],
                "score": 70,
            },
            {
                "cause": "transient_or_version_drift",
                "status": "CANDIDATE",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [],
                "score": 30,
            },
            {
                "cause": "no_simulated_fault",
                "status": "REFUTED",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [health_evidence_id],
                "score": 0,
            },
        ]
    if permitted:
        return [
            {
                "cause": "simulated_target_unhealthy",
                "status": "SUPPORTED",
                "supporting_evidence_ids": [health_evidence_id, version_evidence_id],
                "counter_evidence_ids": [],
                "score": 90,
            },
            {
                "cause": "transient_or_version_drift",
                "status": "REFUTED",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [version_evidence_id],
                "score": 10,
            },
            {
                "cause": "no_simulated_fault",
                "status": "REFUTED",
                "supporting_evidence_ids": [],
                "counter_evidence_ids": [health_evidence_id],
                "score": 0,
            },
        ]
    return [
        {
            "cause": "transient_or_version_drift",
            "status": "SUPPORTED",
            "supporting_evidence_ids": [version_evidence_id],
            "counter_evidence_ids": [health_evidence_id],
            "score": 60,
        },
        {
            "cause": "simulated_target_unhealthy",
            "status": "REFUTED",
            "supporting_evidence_ids": [health_evidence_id],
            "counter_evidence_ids": [version_evidence_id],
            "score": 0,
        },
    ]


def manifest_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
    *,
    prompt_version: str | None = None,
    competing_hypotheses: list[dict] | None = None,
    elapsed_ms: int | None = None,
    lesson_ids: list[str] | None = None,
    memory_mode: str = "NO_MEMORY",
) -> None:
    resolved_prompt = (
        prompt_version
        if prompt_version is not None
        else (
            PROMPT_REVISION_NONE
            if model_id == "deterministic-baseline-v1"
            else PROMPT_REVISION_ADVISORY
        )
    )
    bounded_lessons = list(dict.fromkeys(lesson_ids or []))[:8]
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
                model_id=model_id, prompt_version=resolved_prompt,
                evidence_ids=evidence_ids, cause_scores=scores,
                probe_id=probe_id, decision=decision,
                token_reserved=token_reserved, token_used=token_used,
                competing_hypotheses=competing_hypotheses,
                elapsed_ms=elapsed_ms,
            )
            session.add(source)
            await session.flush()
            await replace_evidence_edges(session, tenant_id, incident_id, "hypothesis", source)

            existing_manifest = await session.scalar(
                select(ContextManifest.id).where(
                    ContextManifest.tenant_id == tenant_id,
                    ContextManifest.incident_id == incident_id,
                    ContextManifest.step_no == step_no,
                )
            )
            if existing_manifest is None:
                manifest_body = {
                    "tenant_id": tenant_id,
                    "incident_id": str(incident_id),
                    "step_no": step_no,
                    "runbook_revision": RUNBOOK_REVISION,
                    "prompt_revision": resolved_prompt,
                    "model_id": model_id,
                    "token_budget_reserved": token_reserved,
                    "token_budget_used": token_used,
                    "evidence_ids": evidence_ids,
                    "lesson_ids": bounded_lessons,
                    "memory_mode": memory_mode,
                }
                session.add(ContextManifest(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    step_no=step_no,
                    runbook_revision=RUNBOOK_REVISION,
                    prompt_revision=resolved_prompt,
                    model_id=model_id,
                    token_budget_reserved=token_reserved,
                    token_budget_used=token_used,
                    evidence_ids=evidence_ids,
                    lesson_ids=bounded_lessons,
                    memory_mode=memory_mode,
                    manifest_digest=manifest_digest(manifest_body),
                ))


async def inspect(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    *,
    advisory_model: str | None = None,
    reuse_legacy_advice: bool = False,
    max_elapsed_seconds: float = DEFAULT_MAX_INVESTIGATION_SECONDS,
    allow_memory: bool = True,
    lesson_ids: list[str] | None = None,
    memory_mode: str | None = None,
    strategy: Literal["ADAPTIVE", "FIXED_RUNBOOK", "CHEAPEST_FIRST"] = "ADAPTIVE",
) -> str:
    """Choose a second read based on the first observation. Never perform an effect."""
    from backend.memory.lessons import retrieve_cited_memory_context

    if strategy in {"FIXED_RUNBOOK", "CHEAPEST_FIRST"}:
        allow_memory = False
        advisory_model = None
        reuse_legacy_advice = False
    started = time.perf_counter()
    if lesson_ids is None or memory_mode is None:
        resolved_lessons: list[str] = []
        resolved_mode = "NO_MEMORY_CONTROL" if not allow_memory else "NO_MEMORY"
        if allow_memory:
            async with sessions() as session:
                target_id = await session.scalar(
                    select(Incident.target_id).where(
                        Incident.id == incident_id, Incident.tenant_id == tenant_id,
                    )
                )
                if target_id is not None:
                    cited, resolved_mode = await retrieve_cited_memory_context(
                        session, tenant_id, target_id, limit=4, allow_memory=allow_memory,
                    )
                    resolved_lessons = [item["lesson_id"] for item in cited]
        lesson_ids = resolved_lessons if lesson_ids is None else lesson_ids
        memory_mode = resolved_mode if memory_mode is None else memory_mode
    try:
        health = await invoke_probe(
            sessions, tenant_id, incident_id, 1, "simulator.health"
        )
    except ProbeDenied:
        await assess_decision(sessions, tenant_id, incident_id, 1, [], handoff_reason="TOOL_BLOCKED")
        return "ABSTAINED"
    step1_ms = max(0, int((time.perf_counter() - started) * 1000))

    ids = [str(health.evidence_id)] if health.evidence_id else []
    health_id = str(health.evidence_id) if health.evidence_id else None
    if health.outcome != "OK":
        await assess_decision(
            sessions, tenant_id, incident_id, 1, ids,
            handoff_reason="TOOL_BLOCKED" if health.outcome == "DENIED" else "TOOL_UNAVAILABLE",
        )
        await write_step(
            sessions, incident_id, 1, ids, "ABSTAINED_NO_SOURCE",
            {}, "simulator.health",
            competing_hypotheses=build_competing_hypotheses(),
            elapsed_ms=step1_ms,
            lesson_ids=lesson_ids, memory_mode=memory_mode,
        )
        return "ABSTAINED"

    current = json.loads(health.excerpt)
    if current.get("healthy", False):
        if strategy == "FIXED_RUNBOOK":
            try:
                version_extra = await invoke_probe(
                    sessions, tenant_id, incident_id, 2, "simulator.version",
                )
                if version_extra.evidence_id:
                    ids.append(str(version_extra.evidence_id))
            except ProbeDenied:
                pass
            await assess_decision(sessions, tenant_id, incident_id, 2, ids, handoff_reason="TARGET_ALREADY_HEALTHY")
            await write_step(
                sessions, incident_id, 2, ids, "ABSTAINED_NO_FAILURE",
                {"no_simulated_fault": 1}, "simulator.version",
                competing_hypotheses=build_competing_hypotheses(
                    health_evidence_id=health_id, healthy=True,
                ),
                elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                lesson_ids=lesson_ids, memory_mode=memory_mode,
            )
            return "ABSTAINED"
        await assess_decision(sessions, tenant_id, incident_id, 1, ids, handoff_reason="TARGET_ALREADY_HEALTHY")
        await write_step(
            sessions, incident_id, 1, ids, "ABSTAINED_NO_FAILURE",
            {"no_simulated_fault": 1}, "simulator.health",
            competing_hypotheses=build_competing_hypotheses(
                health_evidence_id=health_id, healthy=True,
            ),
            elapsed_ms=step1_ms,
            lesson_ids=lesson_ids, memory_mode=memory_mode,
        )
        return "ABSTAINED"

    if time.perf_counter() - started > max_elapsed_seconds:
        await assess_decision(sessions, tenant_id, incident_id, 1, ids, handoff_reason="TOOL_UNAVAILABLE")
        await write_step(
            sessions, incident_id, 1, ids, "ABSTAINED_TIMEOUT",
            {"simulated_target_unhealthy": 1}, "simulator.health",
            competing_hypotheses=build_competing_hypotheses(
                health_evidence_id=health_id, healthy=False,
            ),
            elapsed_ms=step1_ms,
            lesson_ids=lesson_ids, memory_mode=memory_mode,
        )
        return "ABSTAINED"

    advice = None
    if advisory_model is not None or reuse_legacy_advice:
        advice = await advise_after_unhealthy(
            sessions, tenant_id, incident_id, model_id=advisory_model, cache_only=reuse_legacy_advice,
        )
    model_id = advice.model_id if advice else "deterministic-baseline-v1"
    token_used = advice.tokens_used if advice else 0
    token_reserved = advice.tokens_reserved if advice else 0
    step1_hypotheses = build_competing_hypotheses(health_evidence_id=health_id, healthy=False)
    if advice is not None and advice.next_probe == "ABSTAIN":
        await assess_decision(sessions, tenant_id, incident_id, 1, ids, handoff_reason="MODEL_UNCERTAINTY")
        await write_step(
            sessions, incident_id, 1, ids, "MODEL_ADVISORY_ABSTAINED",
            {"simulated_target_unhealthy": 1}, "simulator.health",
            model_id=model_id, token_used=token_used,
            token_reserved=token_reserved,
            competing_hypotheses=step1_hypotheses,
            elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
            lesson_ids=lesson_ids, memory_mode=memory_mode,
        )
        return "ABSTAINED"

    await write_step(
        sessions, incident_id, 1, ids, "NEEDS_SECOND_PROBE",
        {"simulated_target_unhealthy": 1}, "simulator.health",
        model_id=model_id, token_used=token_used,
        token_reserved=token_reserved,
        competing_hypotheses=step1_hypotheses,
        elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        lesson_ids=lesson_ids, memory_mode=memory_mode,
    )
    if time.perf_counter() - started > max_elapsed_seconds:
        await assess_decision(sessions, tenant_id, incident_id, 2, ids, handoff_reason="TOOL_UNAVAILABLE")
        return "ABSTAINED"

    step2_started = time.perf_counter()
    try:
        version = await invoke_probe(
            sessions, tenant_id, incident_id, 2, "simulator.version"
        )
    except ProbeDenied:
        await assess_decision(sessions, tenant_id, incident_id, 2, ids, handoff_reason="TOOL_BLOCKED")
        return "ABSTAINED"
    step2_ms = max(0, int((time.perf_counter() - step2_started) * 1000))
    version_id = str(version.evidence_id) if version.evidence_id else None
    if version.evidence_id:
        ids.append(str(version.evidence_id))
    assessment = await assess_decision(
        sessions, tenant_id, incident_id, 2, ids,
        handoff_reason=(
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
        competing_hypotheses=build_competing_hypotheses(
            health_evidence_id=health_id,
            healthy=False,
            version_evidence_id=version_id,
            permitted=permitted,
        ),
        elapsed_ms=step2_ms,
        lesson_ids=lesson_ids, memory_mode=memory_mode,
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
