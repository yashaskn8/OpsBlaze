"""ToolCatalog for A2 capability-gated read-only tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.domain.tools import MutationToolRejectedError, ToolDefinition, ToolNotFoundError


def _compute_schema_hash(schema: dict[str, Any]) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolCatalog:
    """Server-side authoritative catalog of registered read-only investigation tools."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDefinition] = {}

    def register_tool(self, tool: ToolDefinition) -> None:
        if not tool.read_only:
            raise MutationToolRejectedError(
                f"Mutation tools prohibited in A2 catalog: tool '{tool.id}' has read_only=False"
            )
        self._tools[(tool.id, tool.version)] = tool

    def get_tool(self, tool_id: str, version: str = "v1") -> ToolDefinition:
        key = (tool_id, version)
        if key not in self._tools:
            raise ToolNotFoundError(f"Tool '{tool_id}' version '{version}' not found in catalog")
        return self._tools[key]

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    async def sync_to_db(self, session: Any) -> None:
        """Seed catalog tools into database tool_catalog table."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from backend.persistence.models import ToolCatalog as ToolCatalogModel

        for tool in self._tools.values():
            stmt = (
                pg_insert(ToolCatalogModel)
                .values(
                    id=tool.id,
                    version=tool.version,
                    input_schema_hash=tool.input_schema_hash,
                    output_schema_hash=tool.output_schema_hash,
                    read_only=tool.read_only,
                    max_cost_micros=tool.max_cost_micros,
                    description=tool.description,
                    egress_allowlist=tool.egress_allowlist,
                )
                .on_conflict_do_nothing(index_elements=["id", "version"])
            )
            await session.execute(stmt)

    @classmethod
    def create_default(cls) -> ToolCatalog:
        catalog = cls()

        # 1. Telemetry Sample Tool
        telem_input = {"target_id": "string", "metric": "string", "window_minutes": "integer"}
        telem_output = {"target_id": "string", "samples": "array", "anomalous": "boolean"}
        catalog.register_tool(
            ToolDefinition(
                id="telemetry_sample",
                version="v1",
                input_schema_hash=_compute_schema_hash(telem_input),
                output_schema_hash=_compute_schema_hash(telem_output),
                read_only=True,
                max_cost_micros=1000,
                description="Samples metric timeseries and anomaly indicators from target.",
                egress_allowlist=["local://simulator", "https://api.internal/telemetry"],
            )
        )

        # 2. Log Query Tool
        log_input = {"target_id": "string", "query": "string", "limit": "integer"}
        log_output = {"target_id": "string", "log_lines": "array", "count": "integer"}
        catalog.register_tool(
            ToolDefinition(
                id="log_query",
                version="v1",
                input_schema_hash=_compute_schema_hash(log_input),
                output_schema_hash=_compute_schema_hash(log_output),
                read_only=True,
                max_cost_micros=2000,
                description="Queries recent structured log events for target resource.",
                egress_allowlist=["local://simulator", "https://api.internal/logs"],
            )
        )

        # 3. Config Read Tool
        cfg_input = {"target_id": "string", "section": "string"}
        cfg_output = {"target_id": "string", "config": "object", "version": "string"}
        catalog.register_tool(
            ToolDefinition(
                id="config_read",
                version="v1",
                input_schema_hash=_compute_schema_hash(cfg_input),
                output_schema_hash=_compute_schema_hash(cfg_output),
                read_only=True,
                max_cost_micros=500,
                description="Reads active configuration snapshot and flags for target.",
                egress_allowlist=["local://simulator", "https://api.internal/config"],
            )
        )

        # 4. Health Check Tool
        health_input = {"target_id": "string", "endpoint": "string"}
        health_output = {"target_id": "string", "healthy": "boolean", "latency_ms": "float"}
        catalog.register_tool(
            ToolDefinition(
                id="health_check",
                version="v1",
                input_schema_hash=_compute_schema_hash(health_input),
                output_schema_hash=_compute_schema_hash(health_output),
                read_only=True,
                max_cost_micros=500,
                description="Queries health probe endpoint and latency for target.",
                egress_allowlist=["local://simulator", "https://api.internal/health"],
            )
        )

        return catalog
