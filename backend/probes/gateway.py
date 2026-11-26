"""A2/A3: narrow simulator probes with scoped capabilities, budgets, and provenance."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import utc
from backend.models import Evidence, Incident, ProbeCall, ProbeCapability, SimulatorTarget
from backend.settings import get_settings

CATALOG_VERSION = "simulator-catalog-v1"
ALLOWED_PROBES = {"simulator.health", "simulator.version"}
FRESHNESS_WINDOW = timedelta(minutes=5)
EVIDENCE_RETENTION_DAYS = 30
EXPIRED_EXCERPT = "[EXPIRED_REDACTED]"

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[REDACTED_KEY]"),
    (
        re.compile(r"(?i)\b(password|secret|token|api_key|credential)\s*([:=])\s*([^\s,\"'}]+)"),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"),
        "[REDACTED_PRIVATE_KEY]",
    ),
)

PROBE_CATALOG: dict[str, dict] = {
    "simulator.health": {
        "catalog_id": "simulator.health",
        "catalog_version": CATALOG_VERSION,
        "read_only": True,
        "max_calls_per_incident": 2,
        "timeout_ms": 2000,
        "max_response_bytes": 512,
        "max_concurrent_per_target": 2,
        "rate_limit_per_minute": 30,
        "description": "Read-only simulator target health observation.",
    },
    "simulator.version": {
        "catalog_id": "simulator.version",
        "catalog_version": CATALOG_VERSION,
        "read_only": True,
        "max_calls_per_incident": 2,
        "timeout_ms": 2000,
        "max_response_bytes": 512,
        "max_concurrent_per_target": 2,
        "rate_limit_per_minute": 30,
        "description": "Read-only simulator target version observation.",
    },
}

_ACTIVE_TARGET_PROBES: dict[tuple[str, str], int] = defaultdict(int)


class ProbeDenied(Exception):
    pass


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    evidence_id: UUID | None
    excerpt: str


def catalog_metadata() -> list[dict]:
    """A2-07: read-only metadata for the allowlisted probe catalog."""
    return [dict(PROBE_CATALOG[key]) for key in sorted(PROBE_CATALOG)]


def redact_evidence_excerpt(raw: str, *, max_bytes: int = 512) -> tuple[str, int]:
    """A3-08: strip credential/secret tokens from probe text before persistence."""
    if not isinstance(raw, str):
        raise ProbeDenied("probe payload must be text")
    redacted = raw
    hits = 0
    for pattern, repl in _REDACTION_PATTERNS:
        redacted, count = pattern.subn(repl, redacted)
        hits += count
    if len(redacted.encode("utf-8")) > max_bytes:
        raise ProbeDenied("redacted probe payload exceeds maximum size")
    return redacted, hits


def classify_evidence_freshness(
    row: Evidence, *, now: datetime | None = None,
) -> str:
    """A3-03/A3-08: explicitly label expired, unavailable, fresh, or stale evidence."""
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if row.validity == "EXPIRED" or (
        row.expires_at is not None and utc(row.expires_at) <= observed
    ):
        return "EXPIRED"
    if row.validity != "VALID":
        return "UNAVAILABLE"
    age = observed - utc(row.observed_at)
    if timedelta(0) <= age <= FRESHNESS_WINDOW:
        return "FRESH"
    return "STALE"


def evidence_display(row: Evidence) -> dict[str, str]:
    freshness = classify_evidence_freshness(row)
    return {
        "freshness": freshness,
        "redacted_excerpt": EXPIRED_EXCERPT if freshness == "EXPIRED" else row.redacted_excerpt,
    }


async def issue_incident_capabilities(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    *,
    catalog_ids: list[str] | None = None,
    ttl_seconds: int = 300,
    max_calls: int = 2,
    max_response_bytes: int = 512,
    timeout_ms: int = 2000,
) -> list[ProbeCapability]:
    """A2-02: issue versioned, expiring, revocable capabilities for allowlisted probes."""
    requested = list(sorted(ALLOWED_PROBES)) if catalog_ids is None else list(dict.fromkeys(catalog_ids))
    if not requested or any(item not in ALLOWED_PROBES for item in requested):
        raise ProbeDenied("capability catalog must contain only allowlisted read-only probes")
    if not 1 <= ttl_seconds <= 3600:
        raise ProbeDenied("capability TTL must be 1–3600 seconds")
    if not 1 <= max_calls <= 10:
        raise ProbeDenied("capability max_calls must be 1–10")
    if not 64 <= max_response_bytes <= 512:
        raise ProbeDenied("capability max_response_bytes must be 64–512")
    if not 100 <= timeout_ms <= 10000:
        raise ProbeDenied("capability timeout_ms must be 100–10000")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    issued: list[ProbeCapability] = []
    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
                .with_for_update()
            )
            if incident is None:
                raise ProbeDenied("incident not found in tenant")
            for cid in requested:
                existing = await session.scalar(
                    select(ProbeCapability).where(
                        ProbeCapability.tenant_id == tenant_id,
                        ProbeCapability.incident_id == incident_id,
                        ProbeCapability.catalog_id == cid,
                    ).with_for_update()
                )
                if existing is not None:
                    issued.append(existing)
                    continue
                cap = ProbeCapability(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    catalog_id=cid,
                    catalog_version=CATALOG_VERSION,
                    version=1,
                    max_calls=max_calls,
                    used_calls=0,
                    max_response_bytes=max_response_bytes,
                    timeout_ms=timeout_ms,
                    expires_at=expires,
                )
                session.add(cap)
                issued.append(cap)
    return issued


async def revoke_incident_capability(
    session: AsyncSession,
    tenant_id: str,
    incident_id: UUID,
    catalog_id: str,
    actor_id: str,
) -> ProbeCapability | None:
    """A2-02: revoke a per-incident probe capability at read/call time."""
    if catalog_id not in ALLOWED_PROBES:
        raise ProbeDenied("unsupported probe catalog identifier")
    async with session.begin():
        cap = await session.scalar(
            select(ProbeCapability).where(
                ProbeCapability.tenant_id == tenant_id,
                ProbeCapability.incident_id == incident_id,
                ProbeCapability.catalog_id == catalog_id,
            ).with_for_update()
        )
        if cap is None:
            return None
        if cap.revoked_at is None:
            cap.revoked_at = datetime.now(timezone.utc)
            cap.revoked_by = actor_id[:48]
            cap.version += 1
    return cap


async def list_incident_capabilities(
    session: AsyncSession,
    tenant_id: str,
    incident_id: UUID,
    *,
    now: datetime | None = None,
) -> list[dict] | None:
    incident = await session.scalar(
        select(Incident.id).where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
    )
    if incident is None:
        return None
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = (await session.scalars(
        select(ProbeCapability)
        .where(
            ProbeCapability.tenant_id == tenant_id,
            ProbeCapability.incident_id == incident_id,
        )
        .order_by(ProbeCapability.catalog_id)
    )).all()
    return [
        {
            "id": str(row.id),
            "catalog_id": row.catalog_id,
            "catalog_version": row.catalog_version,
            "version": row.version,
            "max_calls": row.max_calls,
            "used_calls": row.used_calls,
            "max_response_bytes": row.max_response_bytes,
            "timeout_ms": row.timeout_ms,
            "expires_at": utc(row.expires_at).isoformat(),
            "revoked_at": utc(row.revoked_at).isoformat() if row.revoked_at else None,
            "active": (
                row.revoked_at is None
                and utc(row.expires_at) > observed
                and row.used_calls < row.max_calls
            ),
        }
        for row in rows
    ]


async def reserve_external_probe_intent(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    step_no: int,
    catalog_id: str,
    *,
    now: datetime | None = None,
) -> UUID:
    """A2-04: commit durable ProbeCall identity and budget reservation before I/O."""
    if step_no < 1:
        raise ProbeDenied("step must be positive")
    if catalog_id not in ALLOWED_PROBES:
        raise ProbeDenied("probe catalog is not allowlisted")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    meta = PROBE_CATALOG[catalog_id]
    probe_limit = get_settings().max_probe_calls_per_incident
    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident)
                .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
                .with_for_update()
            )
            if incident is None:
                raise ProbeDenied("incident not found in tenant")
            existing = await session.scalar(
                select(ProbeCall).where(
                    ProbeCall.incident_id == incident_id, ProbeCall.step_no == step_no
                )
            )
            if existing is not None:
                raise ProbeDenied("probe call step is already recorded")
            if step_no != incident.probe_used + 1:
                raise ProbeDenied("new probes must use the next sequential step")
            if incident.probe_used >= probe_limit:
                raise ProbeDenied("durable probe budget exhausted")
            cap = await session.scalar(
                select(ProbeCapability).where(
                    ProbeCapability.tenant_id == tenant_id,
                    ProbeCapability.incident_id == incident_id,
                    ProbeCapability.catalog_id == catalog_id,
                ).with_for_update()
            )
            if cap is None:
                cap = ProbeCapability(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    catalog_id=catalog_id,
                    catalog_version=CATALOG_VERSION,
                    version=1,
                    max_calls=max(meta["max_calls_per_incident"], probe_limit),
                    used_calls=0,
                    max_response_bytes=meta["max_response_bytes"],
                    timeout_ms=meta["timeout_ms"],
                    expires_at=observed + timedelta(seconds=300),
                )
                session.add(cap)
                await session.flush()
            if (
                cap.revoked_at is not None
                or utc(cap.expires_at) <= observed
                or cap.used_calls >= cap.max_calls
            ):
                raise ProbeDenied("probe capability expired, revoked, or exhausted")
            incident.probe_used += 1
            cap.used_calls += 1
            call_id = uuid4()
            session.add(
                ProbeCall(
                    id=call_id,
                    incident_id=incident_id,
                    step_no=step_no,
                    catalog_id=catalog_id,
                    outcome="RESERVED",
                    evidence_id=None,
                )
            )
            return call_id


async def invoke_probe(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    step_no: int,
    catalog_id: str,
    *,
    supersedes_evidence_id: UUID | None = None,
    now: datetime | None = None,
) -> ProbeResult:
    """No model-supplied target, URL, credentials, or executable arguments."""
    if step_no < 1:
        raise ProbeDenied("step must be positive")
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_key: tuple[str, str] | None = None
    try:
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
                    ):
                        raise ProbeDenied("recorded probe evidence failed integrity checks")
                    if classify_evidence_freshness(evidence) == "EXPIRED":
                        raise ProbeDenied("recorded probe evidence expired")
                    if hashlib.sha256(evidence.redacted_excerpt.encode()).hexdigest() != evidence.sha256:
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
                probe_limit = get_settings().max_probe_calls_per_incident
                if incident.probe_used >= probe_limit:
                    raise ProbeDenied("durable probe budget exhausted")

                if catalog_id in ALLOWED_PROBES:
                    meta = PROBE_CATALOG[catalog_id]
                    target_key = (tenant_id, incident.target_id)
                    if _ACTIVE_TARGET_PROBES[target_key] >= meta["max_concurrent_per_target"]:
                        target_key = None
                        raise ProbeDenied("target probe concurrency limit reached")
                    _ACTIVE_TARGET_PROBES[target_key] += 1

                incident.probe_used += 1
                if catalog_id not in ALLOWED_PROBES:
                    session.add(
                        ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                                  catalog_id=catalog_id[:64], outcome="DENIED")
                    )
                    return ProbeResult("DENIED", None, "")

                meta = PROBE_CATALOG[catalog_id]
                cap = await session.scalar(
                    select(ProbeCapability).where(
                        ProbeCapability.tenant_id == tenant_id,
                        ProbeCapability.incident_id == incident_id,
                        ProbeCapability.catalog_id == catalog_id,
                    ).with_for_update()
                )
                if cap is None:
                    cap = ProbeCapability(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        incident_id=incident_id,
                        catalog_id=catalog_id,
                        catalog_version=CATALOG_VERSION,
                        version=1,
                        max_calls=max(meta["max_calls_per_incident"], probe_limit),
                        used_calls=0,
                        max_response_bytes=meta["max_response_bytes"],
                        timeout_ms=meta["timeout_ms"],
                        expires_at=observed + timedelta(seconds=300),
                    )
                    session.add(cap)
                    await session.flush()

                # Enforce capability revocation, expiry, call cap, and per-tool minute rate.
                if (
                    cap.revoked_at is not None
                    or utc(cap.expires_at) <= observed
                    or cap.used_calls >= cap.max_calls
                ):
                    session.add(
                        ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                                  catalog_id=catalog_id, outcome="DENIED")
                    )
                    return ProbeResult("DENIED", None, "")

                minute_ago = observed - timedelta(seconds=60)
                recent_calls = int((await session.scalar(
                    select(func.count(ProbeCall.id))
                    .join(Incident, Incident.id == ProbeCall.incident_id)
                    .where(
                        Incident.tenant_id == tenant_id,
                        ProbeCall.catalog_id == catalog_id,
                        ProbeCall.created_at >= minute_ago,
                    )
                )) or 0)
                if recent_calls >= meta["rate_limit_per_minute"]:
                    session.add(
                        ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                                  catalog_id=catalog_id, outcome="DENIED")
                    )
                    return ProbeResult("DENIED", None, "")

                if supersedes_evidence_id is not None:
                    prior = await session.scalar(
                        select(Evidence).where(
                            Evidence.id == supersedes_evidence_id,
                            Evidence.tenant_id == tenant_id,
                            Evidence.incident_id == incident_id,
                            Evidence.source == catalog_id,
                        )
                    )
                    if prior is None:
                        raise ProbeDenied("superseded evidence record not found in incident scope")

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

                raw_excerpt = json.dumps(observation, sort_keys=True, separators=(",", ":"))
                try:
                    excerpt, _ = redact_evidence_excerpt(
                        raw_excerpt, max_bytes=cap.max_response_bytes,
                    )
                except ProbeDenied:
                    session.add(
                        ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                                  catalog_id=catalog_id, outcome="DENIED")
                    )
                    return ProbeResult("DENIED", None, "")

                cap.used_calls += 1
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
                        observed_at=observed,
                        expires_at=observed + timedelta(days=EVIDENCE_RETENTION_DAYS),
                        sha256=digest,
                        redacted_excerpt=excerpt,
                        classification="INTERNAL",
                        validity=validity,
                        supersedes_evidence_id=supersedes_evidence_id,
                    )
                )
                outcome = "OK" if validity == "VALID" else "UNAVAILABLE"
                session.add(
                    ProbeCall(id=uuid4(), incident_id=incident_id, step_no=step_no,
                              catalog_id=catalog_id, outcome=outcome, evidence_id=evidence_id)
                )
                return ProbeResult(outcome, evidence_id, excerpt)
    finally:
        if target_key is not None:
            _ACTIVE_TARGET_PROBES[target_key] = max(0, _ACTIVE_TARGET_PROBES[target_key] - 1)


async def refresh_evidence(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    superseded_evidence_id: UUID,
    *,
    now: datetime | None = None,
) -> ProbeResult:
    """A3-03/A3-06: append-only evidence refresh preserving the historical record."""
    async with sessions() as session:
        prior = await session.scalar(
            select(Evidence).where(
                Evidence.id == superseded_evidence_id,
                Evidence.tenant_id == tenant_id,
                Evidence.incident_id == incident_id,
            )
        )
        incident = await session.scalar(
            select(Incident).where(
                Incident.id == incident_id, Incident.tenant_id == tenant_id,
            )
        )
        if prior is None or incident is None or prior.source not in ALLOWED_PROBES:
            raise ProbeDenied("evidence record cannot be refreshed")
        next_step = incident.probe_used + 1
        source = prior.source
    return await invoke_probe(
        sessions,
        tenant_id,
        incident_id,
        next_step,
        source,
        supersedes_evidence_id=superseded_evidence_id,
        now=now,
    )


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
