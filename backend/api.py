import re
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.schemas import IncidentCreate, IncidentView
from backend.services import IdempotencyConflict, admit_incident, find_incident
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
