from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import (
    Principal, get_principal, local_operator, local_reviewer,
    local_security_admin, local_tenant,
)
from backend.database import get_session
from backend.routers.operations import register_operations_routes
from backend.schemas import IncidentCreate, IncidentView
from backend.services import IdempotencyConflict, RateLimited, admit_incident, find_incident
from backend.settings import Settings, get_settings
from backend.telemetry import initialize_tracing

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    initialize_tracing(get_settings())
    yield


app = FastAPI(title="OpsBlaze", version="0.1.0", lifespan=app_lifespan)


@app.middleware("http")
async def _apply_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/v1/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/v1/auth/me")
async def authenticated_identity(
    principal: Principal = Depends(get_principal),
) -> dict:
    # Never return an email, raw sub, bearer token, or unverified header.
    return {
        "tenant_id": principal.tenant_id,
        "actor_id": principal.subject,
        "roles": sorted(principal.roles),
    }


@app.post("/v1/security/idp-events", status_code=202)
async def accept_signed_idp_revocation(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Dedicated, separately-audienced IdP event JWT; never an API access token."""
    import asyncio
    import jwt
    from sqlalchemy.exc import IntegrityError

    from backend.security.idp_events import verify_identity_event
    from backend.security.revocation import revoke_identity

    if (
        settings.environment != "production"
        or not settings.idp_event_audience
        or settings.idp_event_audience == settings.oidc_audience
    ):
        raise HTTPException(503, "IdP security-event integration not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "signed IdP revocation event required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token or " " in token:
        raise HTTPException(401, "invalid IdP event token")
    try:
        event = await asyncio.to_thread(verify_identity_event, token, settings)
    except (jwt.PyJWTError, ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(401, "invalid signed IdP revocation event") from exc

    try:
        _, affected = await revoke_identity(
            session, event.tenant_id, event.actor_id,
            security_admin="IDP_EVENT",
            reason="Identity provider confirmed access revocation",
            event_digest=event.event_digest,
        )
    except (ValueError, IntegrityError) as exc:
        raise HTTPException(409, "security event replay or identity conflict") from exc
    except Exception as exc:
        raise HTTPException(503, "identity revocation store unavailable") from exc
    return {"status": "recorded", "grants_revoked": affected}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/incidents", response_model=IncidentView, status_code=202)
async def create_incident(
    payload: IncidentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
    settings: Settings = Depends(get_settings),
) -> IncidentView:
    if not idempotency_key.strip():
        raise HTTPException(422, "idempotency key cannot be blank")
    try:
        incident = await admit_incident(
            session, tenant_id, idempotency_key, payload,
            created_by=operator_identity, settings=settings,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    except SQLAlchemyError as exc:
        # Failure to commit admission/outbox is not a 202 acceptance.
        # Do not echo SQL statements, credentials, or DSNs in the error body.
        raise HTTPException(
            503, "admission persistence temporarily unavailable",
            headers={"Retry-After": "30"},
        ) from exc
    return IncidentView.model_validate(incident)


@app.get("/v1/incidents/{incident_id}", response_model=IncidentView)
async def get_incident(
    incident_id: UUID,
    response: Response,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    response.headers["ETag"] = f'"{incident.status_version}"'
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/cancel", response_model=IncidentView, status_code=202)
async def cancel_incident(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> IncidentView:
    from backend.services import VersionConflict, request_cancellation

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the status version")
    try:
        incident = await request_cancellation(session, tenant_id, incident_id, int(version))
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/acknowledge", response_model=IncidentView, status_code=202)
async def acknowledge_incident_review(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> IncidentView:
    from backend.services import VersionConflict, acknowledge_review

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the status version")
    try:
        incident = await acknowledge_review(session, tenant_id, incident_id, int(version))
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)


register_operations_routes(app)


@app.get("/v1/incidents/{incident_id}/investigation")
async def incident_investigation(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    from backend.workbench import read_investigation

    report = await read_investigation(session, tenant_id, incident_id, settings.max_probe_calls_per_incident)
    if report is None:
        raise HTTPException(404, "incident not found")
    return report


async def _record_evidence_access(
    session: AsyncSession,
    tenant_id: str,
    incident_id: UUID,
    actor: str,
    action: str,
    rows: list[tuple[UUID, str]],
) -> None:
    """Persist bounded metadata-only evidence access audits on read paths."""
    from uuid import uuid4
    from backend.models import EvidenceAccessAudit

    if not rows:
        return
    for evidence_id, classification in rows[:50]:
        session.add(
            EvidenceAccessAudit(
                id=uuid4(),
                tenant_id=tenant_id,
                incident_id=incident_id,
                evidence_id=evidence_id,
                actor=actor[:64],
                action=action[:24],
                classification=(classification or "INTERNAL")[:24],
            )
        )
    await session.commit()


@app.get("/v1/incidents/{incident_id}/evidence")
async def incident_evidence(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import Evidence
    from backend.probes.gateway import evidence_display

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(Evidence)
        .where(Evidence.incident_id == incident_id, Evidence.tenant_id == tenant_id)
        .order_by(Evidence.captured_at, Evidence.id)
        .limit(50)
    )).all()
    payload = [
        {"id": str(row.id), "source": row.source,
         "target_version": row.target_version, "sha256": row.sha256,
         "validity": row.validity, **evidence_display(row),
         "supersedes_evidence_id": str(row.supersedes_evidence_id) if row.supersedes_evidence_id else None,
         "expires_at": row.expires_at.isoformat() if row.expires_at else None,
         "captured_at": row.captured_at.isoformat()}
        for row in items
    ]
    await _record_evidence_access(
        session, tenant_id, incident_id, "tenant-reader", "LIST",
        [(row.id, row.classification) for row in items],
    )
    return payload


@app.get("/v1/incidents/{incident_id}/evidence/page")
async def incident_evidence_page(
    incident_id: UUID,
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import Evidence
    from backend.probes.gateway import evidence_display

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    stmt = select(Evidence).where(
        Evidence.incident_id == incident_id, Evidence.tenant_id == tenant_id,
    )
    if cursor is not None:
        stmt = stmt.where(Evidence.id > cursor)
    rows = list((await session.scalars(
        stmt.order_by(Evidence.id).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    payload = {
        "items": [
            {
                "id": str(row.id),
                "source": row.source,
                "source_version": row.source_version,
                "target_version": row.target_version,
                "sha256": row.sha256,
                "validity": row.validity,
                **evidence_display(row),
                "supersedes_evidence_id": (
                    str(row.supersedes_evidence_id) if row.supersedes_evidence_id else None
                ),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "captured_at": row.captured_at.isoformat(),
            }
            for row in visible
        ],
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
    }
    await _record_evidence_access(
        session, tenant_id, incident_id, "tenant-reader", "PAGE",
        [(row.id, row.classification) for row in visible],
    )
    return payload


@app.post("/v1/incidents/{incident_id}/evidence/{evidence_id}/refresh", status_code=201)
async def refresh_incident_evidence(
    incident_id: UUID,
    evidence_id: UUID,
    tenant_id: str = Depends(local_tenant),
    _operator: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.database import get_session_maker
    from backend.probes.gateway import ProbeDenied, refresh_evidence

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    try:
        result = await refresh_evidence(
            get_session_maker(), tenant_id, incident_id, evidence_id,
        )
    except ProbeDenied as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "outcome": result.outcome,
        "evidence_id": str(result.evidence_id) if result.evidence_id else None,
        "supersedes_evidence_id": str(evidence_id),
        "redacted_excerpt": result.excerpt,
    }


@app.get("/v1/incidents/{incident_id}/evidence/lifecycle")
async def incident_evidence_lifecycle(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A3-08: expose aggregate evidence freshness, expiry, plan citations, and access audits."""
    from collections import Counter
    from sqlalchemy import func, select
    from backend.models import Evidence, EvidenceAccessAudit, IncidentPlan
    from backend.probes.gateway import classify_evidence_freshness

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = list((await session.scalars(
        select(Evidence)
        .where(Evidence.tenant_id == tenant_id, Evidence.incident_id == incident_id)
        .order_by(Evidence.captured_at.desc(), Evidence.id)
        .limit(200)
    )).all())
    freshness_counts = Counter(classify_evidence_freshness(row) for row in rows)
    plans = list((await session.scalars(
        select(IncidentPlan)
        .where(IncidentPlan.tenant_id == tenant_id, IncidentPlan.incident_id == incident_id)
        .limit(50)
    )).all())
    cited_ids = {str(ref) for plan in plans for ref in (plan.evidence_ids or [])}
    row_ids = {str(row.id) for row in rows}
    audit_count = int((await session.scalar(
        select(func.count(EvidenceAccessAudit.id)).where(
            EvidenceAccessAudit.tenant_id == tenant_id,
            EvidenceAccessAudit.incident_id == incident_id,
        )
    )) or 0)
    recent_audits = list((await session.scalars(
        select(EvidenceAccessAudit)
        .where(
            EvidenceAccessAudit.tenant_id == tenant_id,
            EvidenceAccessAudit.incident_id == incident_id,
        )
        .order_by(EvidenceAccessAudit.accessed_at.desc(), EvidenceAccessAudit.id)
        .limit(20)
    )).all())
    return {
        "incident_id": str(incident_id),
        "total": len(rows),
        "valid": sum(1 for row in rows if row.validity == "VALID"),
        "fresh": freshness_counts.get("FRESH", 0),
        "stale": freshness_counts.get("STALE", 0),
        "expired": freshness_counts.get("EXPIRED", 0),
        "unavailable": freshness_counts.get("UNAVAILABLE", 0),
        "cited_in_plans": len(cited_ids & row_ids),
        "access_audit_count": audit_count,
        "recent_accesses": [
            {
                "id": str(item.id),
                "evidence_id": str(item.evidence_id),
                "actor": item.actor,
                "action": item.action,
                "classification": item.classification,
                "accessed_at": item.accessed_at.isoformat(),
            }
            for item in recent_audits
        ],
    }


@app.get("/v1/incidents/{incident_id}/context-manifests")
async def incident_context_manifests(
    incident_id: UUID,
    after_step: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import ContextManifest

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = list((await session.scalars(
        select(ContextManifest)
        .where(
            ContextManifest.tenant_id == tenant_id,
            ContextManifest.incident_id == incident_id,
            ContextManifest.step_no > after_step,
        )
        .order_by(ContextManifest.step_no)
        .limit(limit + 1)
    )).all())
    visible = rows[:limit]
    return {
        "manifests": [
            {
                "id": str(row.id),
                "step_no": row.step_no,
                "runbook_revision": row.runbook_revision,
                "prompt_revision": row.prompt_revision,
                "model_id": row.model_id,
                "token_budget_reserved": row.token_budget_reserved,
                "token_budget_used": row.token_budget_used,
                "evidence_ids": row.evidence_ids,
                "lesson_ids": row.lesson_ids,
                "memory_mode": row.memory_mode,
                "manifest_digest": row.manifest_digest,
                "created_at": row.created_at.isoformat(),
            }
            for row in visible
        ],
        "next_after_step": visible[-1].step_no if len(rows) > limit else None,
    }


@app.get("/v1/incidents/{incident_id}/hypotheses")
async def incident_hypotheses(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import HypothesisStep

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(HypothesisStep)
        .where(HypothesisStep.incident_id == incident_id)
        .order_by(HypothesisStep.step_no)
        .limit(50)
    )).all()
    return [
        {"step_no": row.step_no, "decision": row.decision,
         "probe_id": row.probe_id, "model_id": row.model_id,
         "evidence_ids": row.evidence_ids, "cause_scores": row.cause_scores,
         "competing_hypotheses": row.competing_hypotheses or [],
         "elapsed_ms": row.elapsed_ms}
        for row in items
    ]


@app.get("/v1/incidents/{incident_id}/hypotheses/page")
async def incident_hypotheses_page(
    incident_id: UUID,
    after_step: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import HypothesisStep

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = list((await session.scalars(
        select(HypothesisStep)
        .where(
            HypothesisStep.incident_id == incident_id,
            HypothesisStep.step_no > after_step,
        )
        .order_by(HypothesisStep.step_no)
        .limit(limit + 1)
    )).all())
    visible = rows[:limit]
    return {
        "items": [
            {
                "step_no": row.step_no,
                "decision": row.decision,
                "probe_id": row.probe_id,
                "model_id": row.model_id,
                "evidence_ids": row.evidence_ids,
                "cause_scores": row.cause_scores,
                "competing_hypotheses": row.competing_hypotheses or [],
                "elapsed_ms": row.elapsed_ms,
                "token_reserved": row.token_reserved,
                "token_used": row.token_used,
            }
            for row in visible
        ],
        "next_after_step": visible[-1].step_no if len(rows) > limit else None,
    }


@app.get("/v1/incidents/{incident_id}/investigation/metrics")
async def incident_investigation_metrics(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from collections import Counter
    from sqlalchemy import select
    from backend.models import HypothesisStep, ProbeCall, ProbeCapability

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    probes = (await session.scalars(
        select(ProbeCall).where(ProbeCall.incident_id == incident_id).order_by(ProbeCall.step_no)
    )).all()
    steps = (await session.scalars(
        select(HypothesisStep).where(HypothesisStep.incident_id == incident_id).order_by(HypothesisStep.step_no)
    )).all()
    caps = (await session.scalars(
        select(ProbeCapability).where(
            ProbeCapability.tenant_id == tenant_id, ProbeCapability.incident_id == incident_id,
        )
    )).all()
    probe_counts = Counter(p.catalog_id for p in probes)
    denied_count = sum(1 for p in probes if p.outcome == "DENIED")
    leading_causes = [
        max(s.cause_scores.items(), key=lambda kv: kv[1])[0]
        if isinstance(s.cause_scores, dict) and s.cause_scores else None
        for s in steps
    ]
    hypothesis_changes = sum(
        1 for prev, curr in zip(leading_causes, leading_causes[1:]) if prev != curr
    )
    return {
        "incident_id": str(incident_id),
        "probe_calls_total": len(probes),
        "probe_selections": dict(probe_counts),
        "probe_denials": denied_count,
        "hypothesis_steps": len(steps),
        "hypothesis_changes": hypothesis_changes,
        "total_elapsed_ms": sum(s.elapsed_ms or 0 for s in steps),
        "tokens_reserved": sum(s.token_reserved for s in steps),
        "tokens_used": sum(s.token_used for s in steps),
        "capabilities_issued": len(caps),
        "capabilities_revoked": sum(1 for c in caps if c.revoked_at is not None),
    }


@app.post("/v1/incidents/{incident_id}/investigate", status_code=202)
async def trigger_manual_investigation(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    operator: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    from sqlalchemy import select
    from backend.models import Incident, OutboxEntry, UIEvent

    raw_version = if_match.strip('"')
    if not raw_version.isdecimal() or not 0 < len(raw_version) <= 10:
        raise HTTPException(400, "If-Match must be the integer status_version")
    expected_version = int(raw_version)
    async with session.begin():
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if incident is None:
            raise HTTPException(404, "incident not found")
        if incident.status_version != expected_version:
            raise HTTPException(409, "incident status version changed")
        if incident.status_projection not in {"DISPATCH_PENDING", "NEEDS_REVIEW"}:
            raise HTTPException(409, "incident cannot accept a manual investigation trigger in its current state")
        if incident.probe_used >= settings.max_probe_calls_per_incident:
            raise HTTPException(409, "durable probe budget exhausted")
        incident.status_version += 1
        intent = OutboxEntry(
            incident_id=incident.id,
            kind="MANUAL_INVESTIGATE",
            payload={"requested_by": operator[:48], "status_version": incident.status_version},
        )
        session.add(intent)
        session.add(UIEvent(
            tenant_id=tenant_id,
            incident_id=incident.id,
            kind="MANUAL_INVESTIGATION_REQUESTED",
            projection_version=incident.status_version,
        ))
        await session.flush()
        intent_id = intent.id
        status_version = incident.status_version
    return {
        "incident_id": str(incident_id),
        "intent_id": intent_id,
        "status_version": status_version,
        "kind": "MANUAL_INVESTIGATE",
    }


@app.get("/v1/tools/catalog")
async def read_tool_catalog(
    _tenant_id: str = Depends(local_tenant),
) -> dict:
    from backend.probes.gateway import CATALOG_VERSION, catalog_metadata

    return {
        "catalog_version": CATALOG_VERSION,
        "arbitrary_execution_enabled": False,
        "tools": catalog_metadata(),
    }


@app.get("/v1/incidents/{incident_id}/capabilities")
async def get_incident_capabilities(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.probes.gateway import list_incident_capabilities

    caps = await list_incident_capabilities(session, tenant_id, incident_id)
    if caps is None:
        raise HTTPException(404, "incident not found")
    return {"incident_id": str(incident_id), "capabilities": caps}


@app.post("/v1/incidents/{incident_id}/capabilities/{catalog_id}/revoke", status_code=200)
async def revoke_capability(
    incident_id: UUID,
    catalog_id: Literal["simulator.health", "simulator.version"],
    tenant_id: str = Depends(local_tenant),
    operator: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.probes.gateway import revoke_incident_capability

    cap = await revoke_incident_capability(session, tenant_id, incident_id, catalog_id, operator)
    if cap is None:
        raise HTTPException(404, "capability not found")
    return {
        "id": str(cap.id),
        "catalog_id": cap.catalog_id,
        "version": cap.version,
        "revoked_at": cap.revoked_at.isoformat() if cap.revoked_at else None,
    }


@app.get("/v1/incidents/{incident_id}/effects")
async def incident_effects(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import EffectReceipt

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(EffectReceipt)
        .where(EffectReceipt.tenant_id == tenant_id,
               EffectReceipt.incident_id == incident_id)
        .order_by(EffectReceipt.created_at, EffectReceipt.id)
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "state": row.state, "adapter_id": row.adapter_id,
         "target_id": row.target_id, "fence": row.fence,
         "observed_version": row.observed_version}
        for row in items
    ]




class PlanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(default="simulator plan reviewed", min_length=8, max_length=256)


@app.get("/v1/incidents/{incident_id}/plans")
async def incident_plans(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import IncidentPlan

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    plans = (await session.scalars(
        select(IncidentPlan)
        .where(IncidentPlan.incident_id == incident_id, IncidentPlan.tenant_id == tenant_id)
        .limit(10)
    )).all()
    return [
        {"id": str(plan.id), "action_id": plan.action_id,
         "target_id": plan.target_id, "status": plan.status,
         "version": plan.version, "plan_digest": plan.plan_digest,
         "expected_version": plan.expected_version,
         "evidence_ids": plan.evidence_ids}
        for plan in plans
    ]


@app.post("/v1/plans/{plan_id}/decisions", status_code=201)
async def decide_incident_plan(
    plan_id: UUID,
    payload: PlanDecisionRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
    settings: Settings = Depends(get_settings),
) -> dict:
    from backend.plans import PlanConflict, decide_plan

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the plan version")
    try:
        plan = await decide_plan(
            session, tenant_id, plan_id, int(version), payload.decision,
            decided_by=reviewer_identity,
            review_epoch=settings.review_epoch,
            reason=payload.reason,
        )
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if plan is None:
        raise HTTPException(404, "plan not found")
    return {"id": str(plan.id), "status": plan.status, "version": plan.version}


@app.get("/v1/targets/{target_id}/simulator")
async def simulator_target_view(
    target_id: str,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import SimulatorTarget

    target = await session.get(SimulatorTarget, (tenant_id, target_id))
    if target is None:
        raise HTTPException(404, "simulator target not found")
    return {"target_id": target.target_id, "version": target.version,
            "healthy": target.healthy, "adapter": "simulator"}




class OutcomeFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=256)


@app.get("/v1/incidents/{incident_id}/outcomes")
async def incident_outcomes(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import OutcomeRevision

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = (await session.scalars(
        select(OutcomeRevision)
        .where(
            OutcomeRevision.tenant_id == tenant_id,
            OutcomeRevision.incident_id == incident_id,
        )
        .order_by(OutcomeRevision.revision.desc())
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "revision": row.revision,
         "label": row.label, "witness_source": row.witness_source,
         "observed_version": row.observed_version, "reason": row.reason,
         "rule_version": row.rule_version,
         "source_observations": row.source_observations,
         "created_at": row.created_at.isoformat()}
        for row in reversed(rows)
    ]


@app.get("/v1/incidents/{incident_id}/recovery-timing")
async def incident_recovery_timing(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.outcomes import recorded_recovery_timing

    report = await recorded_recovery_timing(session, tenant_id, incident_id)
    if report is None:
        raise HTTPException(404, "incident not found")
    return report


@app.post("/v1/outcomes/{outcome_id}/feedback", status_code=202)
async def report_outcome_dispute(
    outcome_id: UUID,
    payload: OutcomeFeedback,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.outcomes import OutcomeConflict, dispute_outcome

    revision = if_match.strip('"')
    if not revision.isdecimal():
        raise HTTPException(400, "If-Match must be the outcome revision")
    try:
        row = await dispute_outcome(
            session, tenant_id, outcome_id, int(revision), payload.reason,
            reviewer_id=reviewer_identity,
        )
    except OutcomeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "outcome not found")
    return {"id": str(row.id), "revision": row.revision, "label": row.label}


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool


@app.get("/v1/privacy/experience-consent")
async def tenant_experience_consent(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import TenantExperienceConsent

    policy = await session.get(TenantExperienceConsent, tenant_id)
    return {
        "epoch": 0 if policy is None else policy.epoch,
        "allowed": None if policy is None else policy.allowed,
        "policy": "PER_INCIDENT_LEGACY" if policy is None else "TENANT_OVERRIDE",
    }


@app.post("/v1/privacy/experience-consent", status_code=204)
async def update_tenant_experience_consent(
    payload: ConsentRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    security_admin: str = Depends(local_security_admin),
) -> Response:
    from sqlalchemy.exc import IntegrityError
    from backend.experiences import ConsentConflict, set_tenant_consent

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the tenant consent epoch")
    try:
        await set_tenant_consent(session, tenant_id, int(version), payload.allow)
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        # Concurrent first-ever policy insert must fail closed for one writer.
        raise HTTPException(409, "tenant consent version changed") from exc
    return Response(status_code=204)


@app.post("/v1/incidents/{incident_id}/consent", status_code=202)
async def update_experience_consent(
    incident_id: UUID,
    payload: ConsentRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiences import ConsentConflict, set_consent

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the consent epoch")
    try:
        consent = await set_consent(
            session, tenant_id, incident_id, int(version), payload.allow
        )
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if consent is None:
        raise HTTPException(404, "incident not found")
    return {"incident_id": str(incident_id), "epoch": consent.epoch,
            "allowed": consent.allowed}


@app.post("/v1/incidents/{incident_id}/experiences/capture", status_code=201)
async def capture_incident_experience(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiences import ConsentConflict, capture_capsule

    try:
        capsule = await capture_capsule(session, tenant_id, incident_id)
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if capsule is None:
        raise HTTPException(404, "incident not found")
    return {"id": str(capsule.id), "status": capsule.status,
            "consent_epoch": capsule.consent_epoch}


@app.get("/v1/experiences")
async def tenant_experiences(
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiences import read_capsules_page

    rows, next_cursor = await read_capsules_page(
        session, tenant_id, cursor=cursor, limit=limit,
    )
    return {
        "records": [
            {"id": str(row.id), "incident_id": str(row.incident_id),
             "outcome_revision_id": str(row.outcome_revision_id),
             "consent_epoch": row.consent_epoch,
             "tenant_consent_epoch": row.tenant_consent_epoch,
             "target_digest": row.target_digest,
             "evidence_ids": row.evidence_ids,
             "source_step_key": row.source_step_key,
             "provenance": row.provenance,
             "expires_at": row.expires_at.isoformat()}
            for row in rows
        ],
        "next_cursor": next_cursor,
        "redacted": True,
    }


@app.post("/v1/experiences/{capsule_id}/quarantine", status_code=202)
async def quarantine_experience(
    capsule_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiences import ConsentConflict, quarantine_capsule

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the source consent epoch")
    try:
        row = await quarantine_capsule(session, tenant_id, capsule_id, int(version))
    except ConsentConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "experience capsule not found")
    return {
        "id": str(row.id), "status": row.status,
        "consent_epoch": row.consent_epoch,
    }


@app.get("/v1/privacy/export")
async def tenant_privacy_export(
    kind: Literal["consents", "capsules"] = "consents",
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiences import export_privacy_page

    return await export_privacy_page(
        session, tenant_id, kind, cursor=cursor, limit=limit,
    )


class ImprovementDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_cluster_id: UUID
    kind: Literal["PROBE_ORDER", "CONTEXT_LIMIT", "ROUTE_SWITCH"]
    base_policy_id: str = Field(min_length=1, max_length=64)
    change_spec: dict
    hypothesis: str = Field(min_length=8, max_length=512)
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    evaluation_manifest_id: str = Field(min_length=1, max_length=64)


@app.post("/v1/failures/classify/{outcome_id}", status_code=201)
async def classify_failure(
    outcome_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.learning.failures import FailureConflict, record_case

    try:
        case = await record_case(session, tenant_id, outcome_id)
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "outcome not found")
    return {
        "id": str(case.id), "cluster_id": str(case.cluster_id),
        "origin_code": case.origin_code, "review_status": case.review_status,
        "classification_version": case.classification_version,
    }


@app.get("/v1/failures")
async def list_failure_cases(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import FailureCase

    cases = (await session.scalars(
        select(FailureCase).where(FailureCase.tenant_id == tenant_id)
        .order_by(FailureCase.created_at.desc(), FailureCase.id)
        .limit(50)
    )).all()
    return [
        {"id": str(case.id), "incident_id": str(case.incident_id),
         "outcome_revision_id": str(case.outcome_revision_id),
         "cluster_id": str(case.cluster_id), "origin_code": case.origin_code,
         "first_bad_step": case.first_bad_step,
         "review_status": case.review_status,
         "classification_version": case.classification_version}
        for case in cases
    ]


@app.get("/v1/failures/page")
async def failures_page(
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import list_failures_page

    rows, next_cursor = await list_failures_page(
        session, tenant_id, cursor=cursor, limit=limit,
    )
    return {
        "records": [
            {"id": str(case.id), "incident_id": str(case.incident_id),
             "outcome_revision_id": str(case.outcome_revision_id),
             "cluster_id": str(case.cluster_id), "origin_code": case.origin_code,
             "first_bad_step": case.first_bad_step,
             "review_status": case.review_status,
             "classification_version": case.classification_version}
            for case in rows
        ],
        "next_cursor": next_cursor,
    }


@app.get("/v1/failure-clusters")
async def list_failure_clusters(
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import list_clusters_page

    rows, next_cursor = await list_clusters_page(
        session, tenant_id, cursor=cursor, limit=limit,
    )
    return {
        "records": [
            {"id": str(c.id), "signature": c.signature, "version": c.version,
             "case_count": c.case_count, "quarantined": c.quarantined}
            for c in rows
        ],
        "next_cursor": next_cursor,
    }


@app.get("/v1/failure-clusters/{cluster_id}")
async def failure_cluster_view(
    cluster_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import FailureCase, FailureCluster

    cluster = await session.scalar(
        select(FailureCluster).where(
            FailureCluster.id == cluster_id, FailureCluster.tenant_id == tenant_id
        )
    )
    if cluster is None:
        raise HTTPException(404, "cluster not found")
    cases = (await session.scalars(
        select(FailureCase).where(
            FailureCase.cluster_id == cluster.id,
            FailureCase.tenant_id == tenant_id,
        ).order_by(FailureCase.created_at).limit(50)
    )).all()
    return {"id": str(cluster.id), "signature": cluster.signature,
            "version": cluster.version, "case_count": cluster.case_count,
            "quarantined": cluster.quarantined,
            "source_incidents": [str(case.incident_id) for case in cases]}


@app.post("/v1/improvements/draft", status_code=201)
async def create_improvement_draft(
    payload: ImprovementDraft,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.learning.failures import FailureConflict, draft_candidate

    try:
        row = await draft_candidate(
            session, tenant_id, payload.source_cluster_id, payload.kind,
            payload.base_policy_id, payload.change_spec,
            payload.hypothesis, payload.risk, payload.evaluation_manifest_id,
            created_by=operator_identity[:64],
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(row.id), "state": row.state,
            "version": row.version, "kind": row.kind,
            "created_by": row.created_by}


@app.post("/v1/improvements/{candidate_id}/submit", status_code=202)
async def submit_improvement(
    candidate_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.learning.failures import FailureConflict, submit_candidate

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the candidate version")
    try:
        candidate = await submit_candidate(
            session, tenant_id, candidate_id, int(version),
            reviewer_id=reviewer_identity[:64],
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    return {"id": str(candidate.id), "state": candidate.state,
            "version": candidate.version, "reviewed_by": candidate.reviewed_by}


@app.get("/v1/improvements")
async def list_improvements(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import ImprovementCandidate

    rows = (await session.scalars(
        select(ImprovementCandidate)
        .where(ImprovementCandidate.tenant_id == tenant_id)
        .order_by(ImprovementCandidate.created_at.desc())
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "source_cluster_id": str(row.source_cluster_id),
         "kind": row.kind, "base_policy_id": row.base_policy_id,
         "change_spec": row.change_spec, "hypothesis": row.hypothesis,
         "risk": row.risk, "state": row.state, "version": row.version,
         "created_by": row.created_by, "reviewed_by": row.reviewed_by,
         "evaluation_manifest_id": row.evaluation_manifest_id}
        for row in rows
    ]


@app.get("/v1/improvements/page")
async def improvements_page(
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.learning.failures import list_improvements_page

    rows, next_cursor = await list_improvements_page(
        session, tenant_id, cursor=cursor, limit=limit,
    )
    return {
        "records": [
            {"id": str(row.id), "source_cluster_id": str(row.source_cluster_id),
             "kind": row.kind, "base_policy_id": row.base_policy_id,
             "change_spec": row.change_spec, "hypothesis": row.hypothesis,
             "risk": row.risk, "state": row.state, "version": row.version,
             "created_by": row.created_by, "reviewed_by": row.reviewed_by,
             "evaluation_manifest_id": row.evaluation_manifest_id}
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


class LessonDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_capsule_id: UUID
    rule_id: Literal[
        "PROBE_HEALTH_FIRST", "CHECK_VERSION", "ESCALATE_UNCERTAIN"
    ]


@app.post("/v1/lessons", status_code=201)
async def create_lesson_draft(
    payload: LessonDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.memory.lessons import LessonConflict, create_draft

    try:
        lesson = await create_draft(
            session, tenant_id, payload.source_capsule_id, payload.rule_id
        )
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version, "rule_id": lesson.rule_id}


@app.post("/v1/lessons/{lesson_id}/validate", status_code=202)
async def validate_lesson(
    lesson_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.memory.lessons import LessonConflict, validate_draft

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the lesson version")
    try:
        lesson = await validate_draft(session, tenant_id, lesson_id, int(version))
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if lesson is None:
        raise HTTPException(404, "lesson not found")
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version}


@app.post("/v1/lessons/{lesson_id}/revoke", status_code=202)
async def revoke_operational_lesson(
    lesson_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.memory.lessons import LessonConflict, revoke_lesson

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the lesson version")
    try:
        lesson = await revoke_lesson(session, tenant_id, lesson_id, int(version))
    except LessonConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if lesson is None:
        raise HTTPException(404, "lesson not found")
    return {"id": str(lesson.id), "status": lesson.status,
            "version": lesson.version}


@app.get("/v1/lessons")
async def visible_lessons(
    target_id: str,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from backend.memory.lessons import retrieve_active_lessons

    rows = await retrieve_active_lessons(session, tenant_id, target_id)
    return [
        {"id": str(row.id), "rule_id": row.rule_id,
         "summary_redacted": row.summary_redacted,
         "source_outcome_id": str(row.source_outcome_id),
         "version": row.version}
        for row in rows
    ]


class EvaluationRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID


@app.post("/v1/evaluations", status_code=201)
async def register_evaluation(
    payload: EvaluationRegistration,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.evaluation.smoke import EvaluationConflict, freeze
    from backend.learning.failures import FailureConflict

    try:
        manifest = await freeze(session, tenant_id, payload.candidate_id)
    except (EvaluationConflict, FailureConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(manifest.id), "state": manifest.state,
            "fixture_digest": manifest.fixture_digest}


@app.post("/v1/evaluations/{manifest_id}/run", status_code=202)
async def run_evaluation(
    manifest_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.evaluation.smoke import EvaluationConflict, run_smoke

    try:
        manifest = await run_smoke(session, tenant_id, manifest_id)
    except EvaluationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    return {"id": str(manifest.id), "state": manifest.state,
            "promotion_eligible": False}


@app.get("/v1/evaluations/{manifest_id}")
async def get_evaluation(
    manifest_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.evaluation.smoke import report

    result = await report(session, tenant_id, manifest_id)
    if result is None:
        raise HTTPException(404, "manifest not found")
    return result


@app.get("/v1/evaluations/{manifest_id}/trials")
async def evaluation_trials_page(
    manifest_id: UUID,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.evaluation.smoke import read_trials_page

    page = await read_trials_page(
        session, tenant_id, manifest_id, cursor=cursor, limit=limit,
    )
    if page is None:
        raise HTTPException(404, "manifest not found")
    return page


class EvaluationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["ACKNOWLEDGED", "REJECTED_FOR_PROMOTION"]


@app.post("/v1/evaluations/{manifest_id}/review", status_code=202)
async def review_evaluation_manifest(
    manifest_id: UUID,
    payload: EvaluationReviewRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.evaluation.smoke import EvaluationConflict, record_evaluation_review

    expected_state = if_match.strip('"')
    try:
        manifest = await record_evaluation_review(
            session, tenant_id, manifest_id, expected_state,
            payload.decision, reviewer_identity,
        )
    except EvaluationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    return {
        "id": str(manifest.id),
        "state": manifest.state,
        "reviewer_decision": manifest.reviewer_decision,
        "promotion_eligible": False,
    }


@app.get("/v1/incidents/{incident_id}/lineage")
async def incident_lineage(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.lineage import build_lineage

    lineage = await build_lineage(session, tenant_id, incident_id)
    if lineage is None:
        raise HTTPException(404, "incident not found")
    return lineage


@app.get("/v1/incidents/{incident_id}/lineage/records")
async def incident_lineage_records(
    incident_id: UUID,
    kind: Literal[
        "evidence", "hypothesis", "plan", "effect", "outcome",
        "experience", "failure", "lesson", "approval",
    ],
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.lineage import read_lineage_page

    page = await read_lineage_page(
        session, tenant_id, incident_id, kind, cursor, limit,
    )
    if page is None:
        raise HTTPException(404, "incident not found")
    return page


@app.get("/v1/incidents/{incident_id}/lineage/record/{kind}/{record_id}")
async def incident_lineage_record_detail(
    incident_id: UUID,
    kind: Literal[
        "evidence", "hypothesis", "plan", "effect", "outcome",
        "experience", "failure", "lesson", "approval",
    ],
    record_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.lineage import read_lineage_record_detail

    record = await read_lineage_record_detail(
        session, tenant_id, incident_id, kind, record_id,
    )
    if record is None:
        raise HTTPException(404, "lineage record not found")
    if kind == "evidence":
        details = record.get("details") or {}
        await _record_evidence_access(
            session,
            tenant_id,
            incident_id,
            "tenant-reader",
            "DETAIL",
            [(record_id, str(details.get("classification", "INTERNAL")))],
        )
    return record


@app.get("/v1/reservations")
async def reservations_list(
    cursor: str | None = Query(default=None, min_length=1, max_length=256),
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.effects.reservations import list_reservations

    return await list_reservations(session, tenant_id, cursor=cursor, limit=limit)


@app.get("/v1/reservations/{target_id}")
async def reservation_status(
    target_id: str,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.effects.reservations import read_reservation_status

    if not 1 <= len(target_id) <= 256:
        raise HTTPException(422, "invalid target identifier")
    status = await read_reservation_status(session, tenant_id, target_id)
    if status is None:
        raise HTTPException(404, "reservation not found")
    return status


@app.get("/v1/incidents/{incident_id}/traces/{trace_id}")
async def incident_trace(
    incident_id: UUID,
    trace_id: str,
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.trace_refs import read_trace
    from backend.trace_identity import valid_hex_id

    if not valid_hex_id(trace_id, 32):
        raise HTTPException(422, "invalid trace identifier")
    report = await read_trace(session, tenant_id, incident_id, trace_id, cursor, limit)
    if report is None:
        raise HTTPException(404, "trace not found")
    return report


@app.get("/v1/incidents/{incident_id}/route")
async def incident_route(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.routing.decision import read_route

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    decision = await read_route(session, tenant_id, incident_id)
    if decision is None:
        return {"route": "UNROUTED", "policy_id": None}
    return {
        "route": decision.route, "policy_id": decision.policy_id,
        "selection_reason": decision.selection_reason,
        "risk_band": decision.risk_band,
        "cost_cap_probes": decision.cost_cap_probes,
        "router_version": decision.router_version,
        "model_id": decision.model_id,
        "model_token_reservation": decision.model_token_reservation,
        "propensity": decision.propensity,
        "context_digest": decision.context_digest,
        "context_features": decision.context_features,
    }


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: Literal[
        "LOW_CONFIDENCE", "STALE_EVIDENCE", "UNRESOLVED_EFFECT",
        "POLICY_BOUNDARY", "OPERATOR_REQUESTED",
    ] = "OPERATOR_REQUESTED"
    note: str = Field(
        default="Operator escalation requested",
        min_length=8,
        max_length=256,
    )
    next_steps: list[str] = Field(default_factory=list, max_length=5)


_DEFAULT_HANDOFF_STEPS: dict[str, list[str]] = {
    "LOW_CONFIDENCE": ["Inspect cited probe evidence", "Trigger manual simulator verification"],
    "STALE_EVIDENCE": ["Refresh read-only probes", "Confirm target version before approving"],
    "UNRESOLVED_EFFECT": ["Run read-only effect reconciliation", "Review simulator ledger state"],
    "POLICY_BOUNDARY": ["Review pinned policy scope", "Obtain secondary reviewer approval"],
    "OPERATOR_REQUESTED": ["Inspect current evidence & plan preview", "Decide approve or cancel"],
}


@app.post("/v1/incidents/{incident_id}/handoff", status_code=202)
async def request_operator_handoff(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    payload: HandoffRequest | None = None,
    tenant_id: str = Depends(local_tenant),
    operator: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from datetime import datetime, timezone
    from sqlalchemy import select
    from backend.lifecycle import ACTIVE_STATES
    from backend.models import Evidence, Incident, UIEvent

    raw_version = if_match.strip('"')
    if not raw_version.isdecimal() or not 0 < len(raw_version) <= 10:
        raise HTTPException(400, "If-Match must be the integer status_version")
    expected_version = int(raw_version)
    body = payload or HandoffRequest()
    note = body.note.strip()
    if len(note) < 8:
        raise HTTPException(422, "handoff note must contain at least 8 non-space characters")
    steps = [s.strip()[:160] for s in body.next_steps if isinstance(s, str) and s.strip()]
    if not steps:
        steps = list(_DEFAULT_HANDOFF_STEPS[body.reason_code])
    async with session.begin():
        incident = await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id, Incident.tenant_id == tenant_id)
            .with_for_update()
        )
        if incident is None:
            raise HTTPException(404, "incident not found")
        if incident.status_version != expected_version:
            raise HTTPException(409, "incident status version changed")
        if incident.status_projection not in ACTIVE_STATES:
            raise HTTPException(409, "incident is not in an active state")
        cited_evidence = (await session.scalars(
            select(Evidence.id)
            .where(
                Evidence.tenant_id == tenant_id,
                Evidence.incident_id == incident_id,
                Evidence.validity == "VALID",
            )
            .order_by(Evidence.captured_at.desc(), Evidence.id)
            .limit(10)
        )).all()
        incident.status_projection = "NEEDS_REVIEW"
        incident.status_version += 1
        incident.phase_entered_at = datetime.now(timezone.utc)
        session.add(UIEvent(
            tenant_id=tenant_id,
            incident_id=incident.id,
            kind="NEEDS_REVIEW",
            projection_version=incident.status_version,
        ))
        status_version = incident.status_version
    return {
        "incident_id": str(incident_id),
        "status_projection": "NEEDS_REVIEW",
        "status_version": status_version,
        "handoff_by": operator[:48],
        "reason_code": body.reason_code,
        "note": note,
        "next_steps": steps,
        "evidence_ids": [str(eid) for eid in cited_evidence],
    }


@app.get("/v1/adapters/simulator/conformance")
async def simulator_conformance_results(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from sqlalchemy import select
    from backend.models import AdapterConformanceRun

    row = await session.scalar(
        select(AdapterConformanceRun)
        .where(AdapterConformanceRun.adapter_id == "simulator-v1")
        .order_by(AdapterConformanceRun.created_at.desc(), AdapterConformanceRun.id)
        .limit(1)
    )
    if row is None:
        return {"state": "UNTESTED", "production_enablement": False}
    return {
        "run_id": str(row.id), "state": row.state,
        "code_digest": row.code_digest, "suite_version": row.suite_version,
        "retention_seconds": row.retention_seconds,
        "results": row.results, "production_enablement": False,
    }


@app.get("/v1/adapters/{adapter_id}/conformance")
async def adapter_conformance_history(
    adapter_id: str,
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.adapters.conformance import list_adapter_conformance_runs

    if not 1 <= len(adapter_id) <= 48:
        raise HTTPException(422, "invalid adapter identifier")
    return await list_adapter_conformance_runs(
        session, adapter_id, cursor=cursor, limit=limit,
    )


@app.post("/v1/adapters/simulator/conformance/run", status_code=201)
async def run_simulator_conformance_suite(
    tenant_id: str = Depends(local_tenant),
    operator_identity: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from backend.adapters.conformance import execute_suite

    if settings.environment != "development":
        raise HTTPException(403, "lab conformance suite requires development mode")
    bind = session.bind
    await session.rollback()
    if bind is None:
        raise HTTPException(503, "conformance database unavailable")
    run = await execute_suite(async_sessionmaker(bind, expire_on_commit=False))
    return {
        "run_id": str(run.id),
        "adapter_id": run.adapter_id,
        "state": run.state,
        "suite_version": run.suite_version,
        "retention_seconds": run.retention_seconds,
        "triggered_by": operator_identity[:48],
        "production_enablement": False,
    }


class AdapterEnablementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    reason: str = Field(min_length=8, max_length=256)


@app.post("/v1/adapters/{adapter_id}/enablement", status_code=202)
async def update_lab_adapter_enablement(
    adapter_id: str,
    payload: AdapterEnablementRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    security_admin: str = Depends(local_security_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.adapters.conformance import set_lab_adapter_enablement

    raw = if_match.strip('"')
    if not raw.isdecimal():
        raise HTTPException(400, "If-Match must be the adapter enablement version")
    try:
        row = await set_lab_adapter_enablement(
            session, adapter_id, int(raw), payload.enabled,
            security_admin, payload.reason,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "adapter_id": row.adapter_id,
        "scope": row.scope,
        "enabled": row.enabled,
        "version": row.version,
        "updated_by": row.updated_by,
        "production_enablement": False,
    }


class PolicyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    evaluation_manifest_id: UUID


class RolloutDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: UUID
    cohort_percent: int = Field(ge=1, le=5)


@app.post("/v1/policies", status_code=201)
async def create_policy_draft(
    payload: PolicyDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.policy.registry import PolicyConflict, register_bundle
    from backend.learning.failures import FailureConflict

    try:
        bundle = await register_bundle(
            session, tenant_id, payload.candidate_id,
            payload.evaluation_manifest_id,
        )
    except (PolicyConflict, FailureConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(bundle.id), "content_digest": bundle.content_digest,
            "state": bundle.state, "promotion_eligible": False}


@app.post("/v1/rollouts", status_code=201)
async def create_rollout_draft(
    payload: RolloutDraftRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.policy.registry import PolicyConflict, create_rollout

    try:
        rollout = await create_rollout(
            session, tenant_id, payload.bundle_id, payload.cohort_percent
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": str(rollout.id), "state": rollout.state,
            "version": rollout.version, "exposure_enabled": False}


@app.post("/v1/rollouts/{rollout_id}/approve")
async def deny_unvalidated_rollout(
    rollout_id: UUID,
    tenant_id: str = Depends(local_tenant),
    operator_identity: str = Depends(local_operator),
) -> dict:
    raise HTTPException(
        409,
        "release gate closed: independent E5 PASS, signer verification, and "
        "release-owner approval are not implemented",
    )


class RolloutRehearsalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_ids: list[UUID] = Field(min_length=1, max_length=25)
    inject_unsafe: bool = False


@app.post("/v1/rollouts/{rollout_id}/rehearse", status_code=202)
async def rehearse_policy_rollout(
    rollout_id: UUID,
    payload: RolloutRehearsalRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.policy.registry import PolicyConflict, rehearse_simulator_canary

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the rollout version")
    try:
        report = await rehearse_simulator_canary(
            session, tenant_id, rollout_id, int(version),
            payload.incident_ids, inject_unsafe=payload.inject_unsafe,
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if report is None:
        raise HTTPException(404, "rollout not found")
    return report


@app.post("/v1/rollouts/{rollout_id}/{action}", status_code=202)
async def stop_policy_rollout(
    rollout_id: UUID,
    action: Literal["pause", "rollback"],
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.policy.registry import PolicyConflict, stop_rollout

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be the rollout version")
    try:
        rollout = await stop_rollout(
            session, tenant_id, rollout_id, int(version),
            "PAUSED" if action == "pause" else "ROLLED_BACK",
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if rollout is None:
        raise HTTPException(404, "rollout not found")
    return {"id": str(rollout.id), "state": rollout.state,
            "version": rollout.version, "exposure_enabled": False}


@app.post("/v1/rollouts/{rollout_id}/shadow/{incident_id}", status_code=202)
async def record_rollout_shadow(
    rollout_id: UUID,
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    """Rehearsal only: never assign candidate policy or dispatch an effect."""
    from backend.policy.registry import PolicyConflict, record_shadow_observation

    try:
        shadow = await record_shadow_observation(
            session, tenant_id, rollout_id, incident_id,
        )
    except PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if shadow is None:
        raise HTTPException(404, "rollout or incident not found")
    return {
        "id": str(shadow.id),
        "would_assign": shadow.would_assign,
        "preview_only": True,
        "exposure_enabled": False,
    }


@app.get("/v1/rollouts/{rollout_id}/shadow-summary")
async def get_rollout_shadow_summary(
    rollout_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.policy.registry import shadow_summary

    result = await shadow_summary(session, tenant_id, rollout_id)
    if result is None:
        raise HTTPException(404, "rollout not found")
    return result


@app.get("/v1/rollouts/{rollout_id}/preview")
async def preview_policy_rollout(
    rollout_id: UUID,
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import PolicyRollout
    from backend.policy.registry import dry_run_assignment

    rollout = await session.get(PolicyRollout, rollout_id)
    if rollout is None or rollout.tenant_id != tenant_id:
        raise HTTPException(404, "rollout not found")
    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    return {
        "rollout_id": str(rollout.id),
        "would_assign": dry_run_assignment(rollout, tenant_id, incident_id),
        "preview_only": True, "exposure_enabled": False,
    }


@app.get("/v1/incidents")
async def recent_incidents(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[IncidentView]:
    from sqlalchemy import select
    from backend.models import Incident

    rows = (await session.scalars(
        select(Incident)
        .where(Incident.tenant_id == tenant_id)
        .order_by(Incident.created_at.desc(), Incident.id)
        .limit(50)
    )).all()
    return [IncidentView.model_validate(row) for row in rows]


@app.get("/v1/incidents/{incident_id}/events")
async def incident_ui_events(
    incident_id: UUID,
    after_id: int = 0,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.events import ExpiredCursor, read_event_page

    if not 0 <= after_id <= (1 << 63) - 1:
        raise HTTPException(422, "after_id is outside supported cursor range")
    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    try:
        rows = await read_event_page(session, tenant_id, incident_id, after_id)
    except ExpiredCursor as exc:
        raise HTTPException(
            410, {"reason": "event history trimmed; refetch snapshot",
                  "min_after_id": exc.min_after_id},
        ) from exc
    return {
        "events": [
            {"id": event.id, "kind": event.kind,
             "projection_version": event.projection_version,
             "created_at": event.created_at.isoformat()}
            for event in rows
        ],
        "last_id": rows[-1].id if rows else after_id,
    }


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture_id: Literal[
        "recoverable-v1", "false-ack-v1", "already-healthy-v1"
    ]
    seed: int = Field(ge=0, le=1_000_000)


@app.post("/v1/experiments", status_code=201)
async def create_replay_experiment(
    payload: ExperimentRequest,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiments.replay import create_pair

    experiment = await create_pair(session, tenant_id, payload.fixture_id, payload.seed)
    return {"id": str(experiment.id), "status": experiment.status,
            "fixture_digest": experiment.fixture_digest}


@app.get("/v1/experiments")
async def list_replay_experiments(
    cursor: UUID | None = None,
    limit: int = Query(default=25, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiments.replay import list_pairs_page

    rows, next_cursor = await list_pairs_page(
        session, tenant_id, cursor=cursor, limit=limit,
    )
    return {
        "records": [
            {
                "id": str(row.id),
                "fixture_id": row.fixture_id,
                "fixture_digest": row.fixture_digest,
                "seed": row.seed,
                "status": row.status,
                "workload_version": row.workload_version,
                "evidence_policy_version": row.evidence_policy_version,
                "invalidation_reason": row.invalidation_reason,
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
        "measured_ai_comparison": False,
    }


@app.post("/v1/experiments/{experiment_id}/resume", status_code=202)
async def resume_replay_experiment(
    experiment_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.experiments.replay import resume_pair

    experiment = await resume_pair(session, tenant_id, experiment_id)
    if experiment is None:
        raise HTTPException(404, "experiment not found")
    return {
        "id": str(experiment.id),
        "status": experiment.status,
        "invalidation_reason": experiment.invalidation_reason,
        "fixture_digest": experiment.fixture_digest,
    }


@app.get("/v1/experiments/{experiment_id}/results")
async def replay_experiment_results(
    experiment_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.experiments.replay import pair_report

    result = await pair_report(session, tenant_id, experiment_id)
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


@app.get("/v1/incidents/{incident_id}/advisory")
async def incident_model_advisory(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import ModelAdvisory

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    row = await session.get(ModelAdvisory, incident_id)
    if row is None or row.tenant_id != tenant_id:
        return {"state": "DISABLED_OR_NOT_USED"}
    return {
        "state": row.state,
        "model_id": row.model_id,
        "next_probe": row.next_probe,
        "reason_code": row.reason_code,
        "tokens_reserved": row.tokens_reserved,
        "tokens_used": row.tokens_used,
    }


class FailureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrected_code: Literal["PROBE_DENIED", "EFFECT_FAILED", "EFFECT_UNKNOWN"]
    first_bad_step: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=8, max_length=256)


@app.post("/v1/failures/{case_id}/review", status_code=202)
async def review_failure(
    case_id: UUID,
    payload: FailureReviewRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.learning.failures import FailureConflict, review_classification

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be classification version")
    try:
        case = await review_classification(
            session, tenant_id, case_id, int(version),
            payload.corrected_code, payload.first_bad_step, payload.reason,
            reviewer_id=reviewer_identity,
        )
    except FailureConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if case is None:
        raise HTTPException(404, "failure case not found")
    return {
        "id": str(case.id), "cluster_id": str(case.cluster_id),
        "origin_code": case.origin_code,
        "classification_version": case.classification_version,
    }


@app.get("/v1/incidents/{incident_id}/assessments")
async def incident_decision_assessments(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import DecisionAssessment

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    rows = (await session.scalars(
        select(DecisionAssessment)
        .where(
            DecisionAssessment.tenant_id == tenant_id,
            DecisionAssessment.incident_id == incident_id,
        )
        .order_by(DecisionAssessment.step_no)
        .limit(30)
    )).all()
    return [
        {"id": str(row.id), "step_no": row.step_no,
         "feature_version": row.feature_version,
         "threshold_version": row.threshold_version,
         "risk_score": row.risk_score,
         "reason": row.reason, "outcome": row.outcome}
        for row in rows
    ]


@app.get("/v1/incidents/{incident_id}/stream")
async def stream_incident_updates(
    incident_id: UUID,
    request: Request,
    after_id: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    from backend.database import get_session_maker
    from backend.events import (
        STREAM_CAPACITY, ExpiredCursor, stream_incident_events,
        validate_event_cursor,
    )

    if not 0 <= after_id <= (1 << 63) - 1:
        raise HTTPException(400, "after_id is outside supported cursor range")
    if last_event_id is not None:
        if (
            len(last_event_id) > 19
            or not last_event_id.isascii()
            or not last_event_id.isdecimal()
        ):
            raise HTTPException(400, "Last-Event-ID must be a bounded ASCII integer")
        parsed = int(last_event_id)
        if parsed > (1 << 63) - 1:
            raise HTTPException(400, "Last-Event-ID is outside supported cursor range")
        after_id = max(after_id, parsed)
    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    try:
        await validate_event_cursor(session, tenant_id, incident_id, after_id)
    except ExpiredCursor as exc:
        raise HTTPException(
            410, {"reason": "event history trimmed; refetch snapshot",
                  "min_after_id": exc.min_after_id},
        ) from exc

    # The request dependency may outlive StreamingResponse. Release its read
    # transaction before the long-lived stream starts.
    await session.rollback()
    if not STREAM_CAPACITY.claim():
        raise HTTPException(
            503, "incident event stream capacity exhausted",
            headers={"Retry-After": "3"},
        )

    async def bounded_events():
        try:
            async for frame in stream_incident_events(
                get_session_maker(), tenant_id, incident_id, after_id,
                request.is_disconnected,
                actor_id=principal.subject if principal.subject.startswith("oidc:") else None,
                token_expires_at=principal.expires_at,
            ):
                yield frame
        finally:
            STREAM_CAPACITY.release()

    return StreamingResponse(
        bounded_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/incidents/{incident_id}/policy")
async def pinned_incident_policy(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.models import PolicyAssignment

    if await find_incident(session, tenant_id, incident_id) is None:
        raise HTTPException(404, "incident not found")
    assignment = await session.get(PolicyAssignment, (tenant_id, incident_id))
    if assignment is None:
        return {"state": "UNPINNED", "execution_enabled": False}
    return {
        "state": assignment.source,
        "policy_id": assignment.policy_id,
        "bundle_id": str(assignment.bundle_id) if assignment.bundle_id else None,
        "rollout_id": str(assignment.rollout_id) if assignment.rollout_id else None,
        "execution_enabled": assignment.bundle_id is None and assignment.rollout_id is None,
        "assigned_at": assignment.assigned_at.isoformat(),
    }


@app.get("/v1/operations/learning-health")
async def learning_health_metrics(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """E1-08/E2-07/E3-07 aggregate learning health with zero tenant labels."""
    from backend.operations import learning_health

    _ = tenant_id
    return await learning_health(session)


@app.get("/v1/operations/model-budget")
async def tenant_model_budget(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.budgets import current_window
    from backend.models import ModelTokenWindow

    limit = get_settings().ai_token_limit_per_minute
    window = await session.get(ModelTokenWindow, (tenant_id, current_window()))
    return {
        "limit_per_minute": limit,
        "reserved_tokens": window.reserved_tokens if window else 0,
        "reported_used_tokens": window.used_tokens if window else 0,
        "provider_enabled": get_settings().ai_advisory_enabled,
    }


class RevokeApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=256)


@app.get("/v1/plans/{plan_id}")
async def plan_preview(
    plan_id: UUID,
    response: Response,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    from sqlalchemy import and_, select
    from backend.adapters.simulator import utc
    from backend.models import ApprovalGrant, Incident, IncidentPlan, PolicyAssignment

    preview = (await session.execute(
        select(IncidentPlan, ApprovalGrant, PolicyAssignment.policy_id, Incident)
        .join(Incident, and_(
            Incident.id == IncidentPlan.incident_id, Incident.tenant_id == IncidentPlan.tenant_id,
        ))
        .outerjoin(ApprovalGrant, and_(
            ApprovalGrant.plan_id == IncidentPlan.id, ApprovalGrant.tenant_id == IncidentPlan.tenant_id,
        ))
        .outerjoin(PolicyAssignment, and_(
            PolicyAssignment.tenant_id == IncidentPlan.tenant_id,
            PolicyAssignment.incident_id == IncidentPlan.incident_id,
        ))
        .where(
            IncidentPlan.id == plan_id,
            IncidentPlan.tenant_id == tenant_id,
        )
    )).one_or_none()
    if preview is None:
        raise HTTPException(404, "plan not found")
    response.headers["Cache-Control"] = "no-store"
    plan, approval, policy_id, incident = preview
    evaluated_at = datetime.now(UTC)
    expires_at = utc(approval.expires_at) if approval else None
    review_state = approval.state if approval else plan.status
    pending_since = None
    if review_state == "PROPOSED":
        if incident.status_projection == "AWAITING_APPROVAL":
            review_state = "PENDING"
            pending_since = utc(incident.phase_entered_at).isoformat()
        elif incident.status_projection in {"DISPATCH_PENDING", "INVESTIGATING"}:
            review_state = "DRAFT"
        else:
            review_state = "INVALIDATED"
    elif review_state == "APPROVED" and expires_at is not None and expires_at <= evaluated_at:
        review_state = "EXPIRED"
    postcondition = None
    if plan.action_id == "SIMULATOR_MARK_HEALTHY":
        postcondition = {"healthy": True, "version": plan.expected_version + 1}
    return {
        "id": str(plan.id), "incident_id": str(plan.incident_id),
        "action_id": plan.action_id, "target_id": plan.target_id,
        "plan_digest": plan.plan_digest, "expected_version": plan.expected_version,
        "evidence_ids": plan.evidence_ids, "status": plan.status,
        "version": plan.version,
        "policy_id": policy_id,
        "policy_version": f"{policy_id}@epoch-{settings.review_epoch}" if policy_id else None,
        "postcondition": postcondition,
        "review": {
            "state": review_state,
            "pending_since": pending_since,
            "evaluated_at": evaluated_at.isoformat(),
        },
        "approval": (
            {
                "id": str(approval.id),
                "state": approval.state,
                "version": approval.version,
                "expires_at": expires_at.isoformat(),
                "decision": approval.decision,
                "decided_at": utc(approval.created_at).isoformat(),
                "reviewer": approval.reviewer_id,
                "scope": approval.scope,
                "reason": approval.reason,
            }
            if approval else None
        ),
    }


@app.post("/v1/approvals/{approval_id}/revoke", status_code=202)
async def revoke_plan_approval(
    approval_id: UUID,
    payload: RevokeApprovalRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    reviewer_identity: str = Depends(local_reviewer),
) -> dict:
    from backend.plans import PlanConflict, revoke_approval

    version = if_match.strip('"')
    if not version.isdecimal():
        raise HTTPException(400, "If-Match must be approval version")
    try:
        approval = await revoke_approval(
            session, tenant_id, approval_id, int(version),
            reviewer_identity, payload.reason,
        )
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if approval is None:
        raise HTTPException(404, "approval not found")
    return {
        "id": str(approval.id),
        "state": approval.state,
        "version": approval.version,
    }


class RevokeIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=256)


@app.post("/v1/security/identities/{actor_id}/revoke", status_code=202)
async def security_revoke_identity(
    actor_id: str,
    payload: RevokeIdentityRequest,
    tenant_id: str = Depends(local_tenant),
    security_admin: str = Depends(local_security_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    import re
    from backend.security.revocation import revoke_identity

    if not re.fullmatch(r"oidc:[0-9a-f]{32}", actor_id):
        raise HTTPException(422, "invalid actor identifier")
    try:
        row, grants_revoked = await revoke_identity(
            session, tenant_id, actor_id, security_admin, payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {
        "actor_id": row.actor_id, "tenant_id": row.tenant_id,
        "revoked_at": row.revoked_at.isoformat(),
        "grants_revoked": grants_revoked,
    }


@app.post("/v1/effects/{effect_id}/reconcile", status_code=202)
async def request_effect_reconciliation(
    effect_id: UUID,
    tenant_id: str = Depends(local_tenant),
    operator_identity: str = Depends(local_operator),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read-only adapter lookup; never redeliver the original mutation."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.adapters.simulator import SimulatorAdapter
    from backend.effects.gateway import EffectConflict, reconcile_effect
    from backend.models import EffectReceipt

    receipt = await session.scalar(
        select(EffectReceipt).where(
            EffectReceipt.id == effect_id,
            EffectReceipt.tenant_id == tenant_id,
        )
    )
    if receipt is None:
        raise HTTPException(404, "effect receipt not found")
    if receipt.adapter_id != "simulator":
        raise HTTPException(409, "unsupported adapter; manual review required")
    if receipt.state == "PREPARED":
        raise HTTPException(409, "effect was never sent; no status to reconcile")

    # Release the request's read transaction before performing adapter I/O.
    # An isolated session records receipt state after the status lookup.
    bind = session.bind
    await session.rollback()
    if bind is None:
        raise HTTPException(503, "reconciliation database unavailable")
    sessions = async_sessionmaker(bind, expire_on_commit=False)
    try:
        state = await reconcile_effect(
            sessions, SimulatorAdapter(sessions), effect_id,
        )
    except EffectConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"effect_id": str(effect_id), "state": state}
