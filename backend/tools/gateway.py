"""A2 Capability Gateway — server-side enforcement of read-only tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.protocol import TargetAdapterProtocol
from backend.domain.incident import canonical_request_hash
from backend.domain.tools import (
    CapabilityExpiredError,
    CapabilityRevokedError,
    ToolCallRequest,
    ToolCallResult,
    ToolSecurityError,
    ToolStatus,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import CapabilityGrant, ToolCall
from backend.tools.catalog import ToolCatalog
from backend.tools.redactor import SecretRedactor

MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB limit


class CapabilityGateway:
    """Enforces server-side capability grants, egress allowlists, output bounding, and audit recording."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        catalog: ToolCatalog,
        *,
        redactor: SecretRedactor | None = None,
        default_timeout_seconds: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._catalog = catalog
        self._redactor = redactor or SecretRedactor()
        self._default_timeout_seconds = default_timeout_seconds

    async def execute_tool(
        self,
        request: ToolCallRequest,
        adapter: TargetAdapterProtocol,
        *,
        now: datetime | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolCallResult:
        """Verify capability grant and execute read-only tool with strict safety constraints."""
        call_id = uuid.uuid4()
        check_time = now or datetime.now(UTC)
        start_mono = time.monotonic()
        tool = self._catalog.get_tool(request.tool_id, request.tool_version)

        # 1. Server-side Capability Grant Verification
        async with self._session_factory() as session:
            grant = (
                await session.execute(
                    select(CapabilityGrant).where(CapabilityGrant.id == request.grant_id)
                )
            ).scalar_one_or_none()

            if grant is None:
                raise ToolSecurityError(f"Capability grant '{request.grant_id}' does not exist")

            if grant.tenant_id != request.tenant_id:
                raise ToolSecurityError("Cross-tenant capability grant violation")

            if grant.incident_id != request.incident_id:
                raise ToolSecurityError("Cross-incident capability grant violation")

            if grant.target_id != request.target_id:
                raise ToolSecurityError(
                    f"Target mismatch: grant is for '{grant.target_id}', request is for '{request.target_id}'"
                )

            if grant.tool_id != request.tool_id:
                raise ToolSecurityError(
                    f"Tool mismatch: grant is for '{grant.tool_id}', request is for '{request.tool_id}'"
                )

            if grant.expires_at <= check_time:
                raise CapabilityExpiredError(f"Capability grant '{grant.id}' expired at {grant.expires_at}")

            if grant.revoked_at is not None:
                raise CapabilityRevokedError(f"Capability grant '{grant.id}' was revoked at {grant.revoked_at}")

        # 2. SSRF & Egress Destination Check
        if "endpoint" in request.parameters:
            endpoint = str(request.parameters["endpoint"])
            if not any(endpoint.startswith(allowed) for allowed in tool.egress_allowlist):
                raise ToolSecurityError(f"Egress destination rejected: '{endpoint}' not in egress allowlist")

        args_digest = canonical_request_hash(request.parameters)

        # 3. Safe Dispatch to Target Adapter with Timeout
        timeout = timeout_seconds or self._default_timeout_seconds
        raw_output: dict[str, Any]
        status = ToolStatus.COMPLETED
        error_detail: str | None = None

        try:
            raw_output = await asyncio.wait_for(
                self._dispatch_read_only_adapter(request, adapter),
                timeout=timeout,
            )
        except TimeoutError:
            status = ToolStatus.TIMED_OUT
            raw_output = {"error": f"Tool execution timed out after {timeout} seconds"}
            error_detail = "Execution timeout"
        except Exception as exc:  # noqa: BLE001
            status = ToolStatus.FAILED
            raw_output = {"error": str(exc)}
            error_detail = str(exc)

        # 4. Secret Redaction
        redacted_output = self._redactor.redact_dict(raw_output)

        # 5. Semantic Output Bounding (64 KB Envelope)
        bounded_output = self._bound_output(redacted_output)
        canonical_output = json.dumps(bounded_output, sort_keys=True, separators=(",", ":"))
        output_digest = hashlib.sha256(canonical_output.encode("utf-8")).hexdigest()

        duration_ms = round((time.monotonic() - start_mono) * 1000.0, 2)

        # 6. Audit Trail Persistence
        async with self._session_factory() as session:
            record = ToolCall(
                id=call_id,
                tenant_id=request.tenant_id,
                incident_id=request.incident_id,
                step_no=request.step_no,
                grant_id=request.grant_id,
                tool_id=request.tool_id,
                tool_version=request.tool_version,
                target_id=request.target_id,
                status=status.value,
                args_digest=args_digest,
                args_payload=request.parameters,
                output_digest=output_digest,
                result_evidence_id=None,
                duration_ms=duration_ms,
                error_detail=error_detail,
                created_at=check_time,
            )
            session.add(record)
            await session.commit()

        metrics.inc("tool_calls_total")
        metrics.inc(f"tool_calls_{status.value.lower()}_total")
        emit_event(
            "tool_call.completed",
            tenant_id=request.tenant_id,
            incident_id=request.incident_id,
            tool_id=request.tool_id,
            target_id=request.target_id,
            status=status.value,
            duration_ms=duration_ms,
        )

        return ToolCallResult(
            call_id=call_id,
            status=status,
            args_digest=args_digest,
            output_digest=output_digest,
            result_evidence_id=None,
            payload=bounded_output,
            duration_ms=duration_ms,
            error_detail=error_detail,
        )

    async def _dispatch_read_only_adapter(
        self, request: ToolCallRequest, adapter: TargetAdapterProtocol
    ) -> dict[str, Any]:
        """Dispatch typed read-only tool against adapter."""
        # Query target resource status
        snap = await adapter.read_resource(request.tenant_id, request.target_id)
        
        if request.tool_id == "telemetry_sample":
            metric = request.parameters.get("metric", "cpu_utilization")
            return {
                "target_id": request.target_id,
                "current_version": snap.current_version,
                "metric": metric,
                "samples": [
                    {"t": -60, "value": 45.2},
                    {"t": -30, "value": 78.4},
                    {"t": 0, "value": 94.1},
                ],
                "anomalous": True,
            }
        elif request.tool_id == "log_query":
            query = request.parameters.get("query", "error")
            limit = min(100, int(request.parameters.get("limit", 20)))
            return {
                "target_id": request.target_id,
                "query": query,
                "count": 3,
                "log_lines": [
                    {"level": "ERROR", "msg": f"Connection pool exhausted on {request.target_id}"},
                    {"level": "WARN", "msg": "Latency threshold 500ms breached"},
                    {"level": "INFO", "msg": "Health probe timeout"},
                ][:limit],
            }
        elif request.tool_id == "config_read":
            section = request.parameters.get("section", "all")
            return {
                "target_id": request.target_id,
                "section": section,
                "version": snap.current_version,
                "config": {
                    "max_connections": 100,
                    "timeout_ms": 3000,
                    "pool_mode": "transaction",
                },
            }
        elif request.tool_id == "health_check":
            return {
                "target_id": request.target_id,
                "healthy": False,
                "last_effect_id": snap.last_effect_id,
                "highest_fence_seen": snap.highest_fence_seen,
                "latency_ms": 124.5,
            }
        else:
            return {
                "target_id": request.target_id,
                "version": snap.current_version,
                "data": {},
            }

    def _bound_output(self, data: dict[str, Any]) -> dict[str, Any]:
        """Ensure serialized JSON output does not exceed 64 KB."""
        serialized = json.dumps(data, separators=(",", ":"))
        byte_len = len(serialized.encode("utf-8"))
        if byte_len <= MAX_OUTPUT_BYTES:
            return data

        # Wrap in semantic truncation envelope
        return {
            "truncated": True,
            "original_byte_length": byte_len,
            "max_allowed_bytes": MAX_OUTPUT_BYTES,
            "sample": {k: v for i, (k, v) in enumerate(data.items()) if i < 3},
        }
