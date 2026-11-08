"""FastAPI application entrypoint for OpsBlaze."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.api.approvals import router as approvals_router
from backend.api.auth import router as auth_router
from backend.api.evaluations import router as evaluations_router
from backend.api.experience import router as experience_router
from backend.api.improvements import router as improvements_router
from backend.api.incidents import router as incidents_router
from backend.api.lessons import router as lessons_router
from backend.api.outcomes import router as outcomes_router
from backend.api.releases import router as releases_router
from backend.api.trace import router as trace_router
from backend.api.workbench import router as workbench_router
from backend.config import Settings, get_settings
from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.auth import AuthenticationError, AuthorizationForbiddenError
from backend.observability.metrics import (
    correlation_id_ctx,
    metrics,
    redact_value,
)
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.repositories import IncidentRepository

# Default local lab tenant for interactive React development (registered on startup if DB is up)
DEFAULT_LAB_TENANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
DEFAULT_LAB_TENANT_NAME = "opsblaze-local-lab"

MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KiB payload size cap


def _ensure_app_engine(app_instance: FastAPI, settings: Settings | None = None) -> AsyncEngine:
    existing: AsyncEngine | None = getattr(app_instance.state, "engine", None)
    if existing is not None:
        return existing
    cfg = settings or get_settings()
    engine = make_engine(cfg)
    app_instance.state.engine = engine
    app_instance.state.session_factory = make_session_factory(engine)
    return engine


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    engine = _ensure_app_engine(app_instance)
    try:
        repo = IncidentRepository(app_instance.state.session_factory)
        await repo.ensure_tenant(DEFAULT_LAB_TENANT_ID, DEFAULT_LAB_TENANT_NAME)
    except (SQLAlchemyError, OSError):
        # Allow app startup even if DB is temporarily unreachable; /readyz reports readiness
        pass
    try:
        yield
    finally:
        if getattr(app_instance.state, "engine", None) is not None:
            await engine.dispose()
            app_instance.state.engine = None
            app_instance.state.session_factory = None


def create_app(settings: Settings | None = None) -> FastAPI:
    app_instance = FastAPI(title="OpsBlaze", lifespan=lifespan)
    _ensure_app_engine(app_instance, settings)

    @app_instance.middleware("http")
    async def correlation_and_size_guard(request: Request, call_next: Any) -> Response:
        raw_cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        clean_cid = str(redact_value(raw_cid))[:128]
        token = correlation_id_ctx.set(clean_cid)
        try:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_REQUEST_BODY_BYTES:
                        return JSONResponse(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            content={
                                "detail": "Request payload exceeds maximum allowed size",
                                "correlation_id": clean_cid,
                            },
                            headers={"X-Correlation-ID": clean_cid},
                        )
                except ValueError:
                    pass

            body = await request.body()
            if len(body) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "detail": "Request payload exceeds maximum allowed size",
                        "correlation_id": clean_cid,
                    },
                    headers={"X-Correlation-ID": clean_cid},
                )

            response: Response = await call_next(request)
            response.headers["X-Correlation-ID"] = clean_cid
            return response
        finally:
            correlation_id_ctx.reset(token)

    @app_instance.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        cid = correlation_id_ctx.get() or str(uuid.uuid4())
        safe_errors = []
        for err in exc.errors():
            safe_errors.append(
                {
                    "loc": list(err.get("loc", ())),
                    "msg": str(redact_value(err.get("msg", "Validation error"))),
                    "type": str(err.get("type", "value_error")),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": safe_errors, "correlation_id": cid},
            headers={"X-Correlation-ID": cid},
        )

    @app_instance.exception_handler(ApprovalRoleForbiddenError)
    async def approval_role_forbidden_handler(
        request: Request, exc: ApprovalRoleForbiddenError
    ) -> JSONResponse:
        cid = correlation_id_ctx.get() or str(uuid.uuid4())
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc), "correlation_id": cid},
            headers={"X-Correlation-ID": cid},
        )

    @app_instance.exception_handler(AuthorizationForbiddenError)
    async def authorization_forbidden_handler(
        request: Request, exc: AuthorizationForbiddenError
    ) -> JSONResponse:
        cid = correlation_id_ctx.get() or str(uuid.uuid4())
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc), "correlation_id": cid},
            headers={"X-Correlation-ID": cid},
        )

    @app_instance.exception_handler(AuthenticationError)
    async def authentication_error_handler(
        request: Request, exc: AuthenticationError
    ) -> JSONResponse:
        cid = correlation_id_ctx.get() or str(uuid.uuid4())
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc), "correlation_id": cid},
            headers={"X-Correlation-ID": cid},
        )

    @app_instance.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        cid = correlation_id_ctx.get() or str(uuid.uuid4())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Internal server error",
                "correlation_id": cid,
            },
            headers={"X-Correlation-ID": cid},
        )

    @app_instance.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app_instance.get("/readyz", response_model=None)
    async def readyz(request: Request) -> dict[str, str] | JSONResponse:
        engine = _ensure_app_engine(request.app)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
        except (SQLAlchemyError, OSError, RuntimeError):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "degraded", "database": "unavailable"},
            )
        return {"status": "ready", "database": "ok"}

    @app_instance.get("/metrics")
    async def get_metrics() -> dict[str, Any]:
        return metrics.snapshot()

    app_instance.include_router(incidents_router)
    app_instance.include_router(auth_router)
    app_instance.include_router(approvals_router)
    app_instance.include_router(workbench_router)
    app_instance.include_router(trace_router)
    app_instance.include_router(experience_router)
    app_instance.include_router(outcomes_router)
    app_instance.include_router(improvements_router, prefix="/v1")
    app_instance.include_router(lessons_router, prefix="/v1")
    app_instance.include_router(evaluations_router, prefix="/v1")
    app_instance.include_router(releases_router)
    return app_instance


app = create_app()
