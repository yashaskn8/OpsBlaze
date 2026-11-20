"""A2/A3: narrow simulator probes with scoped identity, budget, and provenance."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import Evidence, Incident, ProbeCall, SimulatorTarget
from backend.settings import get_settings

ALLOWED_PROBES = {"simulator.health", "simulator.version"}


class ProbeDenied(Exception):
    pass


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    evidence_id: UUID | None
    excerpt: str


async def invoke_probe(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    step_no: int,
    catalog_id: str,
) -> ProbeResult:
    """No model-supplied target, URL, credentials, or executable arguments."""
    if step_no < 1:
        raise ProbeDenied("step must be positive")
    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
                .with_for_update()
            )
            if incident is None:
                raise ProbeDenied("incident not found in tenant")
            recorded = await session.scalar(
                select(ProbeCall).where(
                    ProbeCall.incident_id == incident_id, ProbeCall.step_no == step_no
                )
            )
            if recorded is not None:
                if recorded.catalog_id != catalog_id:
                    raise ProbeDenied("probe identity changed for recorded step")
                if recorded.outcome == "DENIED" and recorded.evidence_id is None:
                    return ProbeResult("DENIED", None, "")
                if (
                    catalog_id not in ALLOWED_PROBES
                    or recorded.outcome not in {"OK", "UNAVAILABLE"}
                    or recorded.evidence_id is None
                ):
                    raise ProbeDenied("recorded probe result is inconsistent")
                evidence = await session.get(Evidence, recorded.evidence_id)
                if (
                    evidence is None
                    or evidence.tenant_id != tenant_id
                    or evidence.incident_id != incident_id
                    or evidence.target_id != incident.target_id
                    or evidence.source != catalog_id
                    or evidence.source_version != "simulator-v1"
                    or evidence.classification != "INTERNAL"
                    or hashlib.sha256(evidence.redacted_excerpt.encode()).hexdigest()
                        != evidence.sha256
                ):
                    raise ProbeDenied("recorded probe evidence failed integrity checks")
                try:
                    observation = json.loads(evidence.redacted_excerpt)
                except (TypeError, ValueError) as exc:
                    raise ProbeDenied("recorded probe observation is malformed") from exc
                if recorded.outcome == "OK":
                    if evidence.validity != "VALID" or type(evidence.target_version) is not int:
                        raise ProbeDenied("recorded valid probe has inconsistent metadata")
                    expected = (
                        {"healthy": observation.get("healthy")}
                        if catalog_id == "simulator.health" and isinstance(observation, dict)
                        else {"version": evidence.target_version}
                    )
                    if (
                        observation != expected
                        or (
                            catalog_id == "simulator.health"
                            and type(observation.get("healthy")) is not bool
                        )
                        or (
                            catalog_id == "simulator.version"
                            and (
                                not isinstance(observation, dict)
                                or type(observation.get("version")) is not int
                            )
                        )
                    ):
                        raise ProbeDenied("recorded probe observation failed schema checks")
                elif (
                    evidence.validity != "INVALID"
                    or evidence.target_version is not None
                    or observation != {"status": "UNAVAILABLE"}
                ):
                    raise ProbeDenied("recorded unavailable probe is inconsistent")
                return ProbeResult(recorded.outcome, evidence.id, evidence.redacted_excerpt)

            if step_no != incident.probe_used + 1:
                raise ProbeDenied("new probes must use the next sequential step")
            if incident.probe_used >= get_settings().max_probe_calls_per_incident:
                raise ProbeDenied("durable probe budget exhausted")

            incident.probe_used += 1
            if catalog_id not in ALLOWED_PROBES:
                session.add(
                    ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                              catalog_id=catalog_id[:64], outcome="DENIED")
                )
                return ProbeResult("DENIED", None, "")

            target = await session.get(
                SimulatorTarget, (tenant_id, incident.target_id)
            )
            if target is None:
                version = None
                observation = {"status": "UNAVAILABLE"}
                validity = "INVALID"
            elif catalog_id == "simulator.health":
                version = target.version
                observation = {"healthy": target.healthy}
                validity = "VALID"
            else:
                version = target.version
                observation = {"version": target.version}
                validity = "VALID"

            excerpt = json.dumps(observation, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            evidence_id = uuid4()
            session.add(
                Evidence(
                    id=evidence_id,
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    target_id=incident.target_id,
                    source=catalog_id,
                    source_version="simulator-v1",
                    target_version=version,
                    observed_at=datetime.now(timezone.utc),
                    sha256=digest,
                    redacted_excerpt=excerpt,
                    classification="INTERNAL",
                    validity=validity,
                )
            )
            outcome = "OK" if validity == "VALID" else "UNAVAILABLE"
            session.add(
                ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                          catalog_id=catalog_id, outcome=outcome, evidence_id=evidence_id)
            )
            return ProbeResult(outcome, evidence_id, excerpt)


async def evidence_links_intact(
    session: AsyncSession, incident_id: UUID, evidence_ids: list[UUID],
) -> bool:
    """A3: bind the exact assessment/plan evidence to its two authorized probe steps."""
    if len(evidence_ids) != 2 or len(set(evidence_ids)) != 2:
        return False
    rows = (await session.scalars(
        select(ProbeCall).where(
            ProbeCall.incident_id == incident_id,
            ProbeCall.step_no.in_((1, 2)),
        )
    )).all()
    links = {
        (row.step_no, row.catalog_id, row.outcome, row.evidence_id)
        for row in rows
    }
    return links == {
        (1, "simulator.health", "OK", evidence_ids[0]),
        (2, "simulator.version", "OK", evidence_ids[1]),
    }
