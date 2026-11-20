import re
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.schemas import IncidentCreate, IncidentView
from backend.services import IdempotencyConflict, RateLimited, admit_incident, find_incident
from backend.settings import Settings, get_settings

app = FastAPI(title="OpsBlaze", version="0.1.0")


def local_tenant(
    x_tenant_id: Annotated[str, Header(min_length=1, max_length=64)],
    x_opsblaze_token: Annotated[str, Header()],
    settings: Settings = Depends(get_settings),
) -> str:
    # Local-only bootstrap authentication. Do not deploy as multi-tenant auth.
    if settings.environment != "development" or not settings.local_token:
        raise HTTPException(503, "local API disabled; production auth not implemented")
    if not secrets.compare_digest(x_opsblaze_token, settings.local_token):
        raise HTTPException(401, "invalid local API token")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", x_tenant_id) is None:
        raise HTTPException(422, "invalid tenant identifier")
    return x_tenant_id


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/incidents", response_model=IncidentView, status_code=202)
async def create_incident(
    payload: IncidentCreate,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    if not idempotency_key.strip():
        raise HTTPException(422, "idempotency key cannot be blank")
    try:
        incident = await admit_incident(session, tenant_id, idempotency_key, payload)
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    return IncidentView.model_validate(incident)


@app.get("/v1/incidents/{incident_id}", response_model=IncidentView)
async def get_incident(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> IncidentView:
    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    return IncidentView.model_validate(incident)

@app.post("/v1/incidents/{incident_id}/cancel", response_model=IncidentView, status_code=202)
async def cancel_incident(
    incident_id: UUID,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
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


@app.get("/v1/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    from sqlalchemy import text
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready"}


@app.get("/v1/operations/queues")
async def tenant_queues(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    from sqlalchemy import func, select
    from backend.models import Incident, OutboxEntry

    pending = await session.scalar(
        select(func.count())
        .select_from(OutboxEntry)
        .join(Incident, Incident.id == OutboxEntry.incident_id)
        .where(Incident.tenant_id == tenant_id, OutboxEntry.delivered_at.is_(None))
    )
    active = await session.scalar(
        select(func.count())
        .select_from(Incident)
        .where(
            Incident.tenant_id == tenant_id,
            Incident.status_projection.in_(
                ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
                 "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED"]
            ),
        )
    )
    return {"pending_outbox": pending or 0, "active_incidents": active or 0}


@app.get("/v1/incidents/{incident_id}/evidence")
async def incident_evidence(
    incident_id: UUID,
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select
    from backend.models import Evidence

    incident = await find_incident(session, tenant_id, incident_id)
    if incident is None:
        raise HTTPException(404, "incident not found")
    items = (await session.scalars(
        select(Evidence)
        .where(Evidence.incident_id == incident_id, Evidence.tenant_id == tenant_id)
        .order_by(Evidence.captured_at, Evidence.id)
        .limit(50)
    )).all()
    return [
        {"id": str(row.id), "source": row.source,
         "target_version": row.target_version, "sha256": row.sha256,
         "validity": row.validity, "redacted_excerpt": row.redacted_excerpt,
         "captured_at": row.captured_at.isoformat()}
        for row in items
    ]


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
         "evidence_ids": row.evidence_ids, "cause_scores": row.cause_scores}
        for row in items
    ]


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
