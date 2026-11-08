"""Typed adapter protocol, capability profile, and deterministic code digest for Feature E8."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from backend.domain.incident import canonical_request_hash

SUPPORTED_SIMULATOR_ACTIONS: tuple[str, ...] = (
    "restart_service",
    "drain_traffic",
    "scale_workers",
    "rollback_config",
)


class AdapterApplyStatus(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE_APPLIED = "DUPLICATE_APPLIED"
    REJECTED_STALE_FENCE = "REJECTED_STALE_FENCE"
    REJECTED_VERSION_CONFLICT = "REJECTED_VERSION_CONFLICT"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class AdapterLookupStatus(StrEnum):
    COMMITTED = "COMMITTED"
    NOT_FOUND = "NOT_FOUND"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    RETENTION_EXPIRED = "RETENTION_EXPIRED"


@dataclass(frozen=True)
class AdapterCapabilityProfile:
    adapter_id: str
    version: str
    code_digest: str
    supports_idempotency: bool = True
    supports_fencing: bool = True
    supports_version_cas: bool = True
    supports_status_lookup: bool = True
    retention_horizon_seconds: int = 300
    is_simulator: bool = True
    supported_actions: tuple[str, ...] = SUPPORTED_SIMULATOR_ACTIONS
    supported_fault_scenarios: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "version": self.version,
            "code_digest": self.code_digest,
            "supports_idempotency": self.supports_idempotency,
            "supports_fencing": self.supports_fencing,
            "supports_version_cas": self.supports_version_cas,
            "supports_status_lookup": self.supports_status_lookup,
            "retention_horizon_seconds": self.retention_horizon_seconds,
            "is_simulator": self.is_simulator,
            "supported_actions": list(self.supported_actions),
            "supported_fault_scenarios": list(self.supported_fault_scenarios),
        }


@dataclass(frozen=True)
class AdapterApplyRequest:
    tenant_id: uuid.UUID
    target_id: str
    effect_id: str
    fence: int
    expected_version: str
    action_kind: str
    action_digest: str
    parameters: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""


@dataclass(frozen=True)
class AdapterApplyResponse:
    status: AdapterApplyStatus
    external_ref: str | None = None
    observed_version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class AdapterEffectLookupResult:
    status: AdapterLookupStatus
    external_ref: str | None = None
    observed_version: str | None = None
    fence: int | None = None
    action_digest: str | None = None
    committed_at: datetime | None = None
    detail: str = ""


@dataclass(frozen=True)
class ResourceStateSnapshot:
    target_id: str
    current_version: str
    highest_fence_seen: int
    last_effect_id: str | None
    state_data: dict[str, Any]
    healthy: bool = True


@runtime_checkable
class TargetAdapterProtocol(Protocol):
    def profile(self) -> AdapterCapabilityProfile: ...

    async def apply(self, request: AdapterApplyRequest) -> AdapterApplyResponse: ...

    async def query_effect(
        self, tenant_id: uuid.UUID, target_id: str, effect_id: str
    ) -> AdapterEffectLookupResult: ...

    async def read_resource(
        self, tenant_id: uuid.UUID, target_id: str
    ) -> ResourceStateSnapshot: ...


def compute_adapter_code_digest(
    adapter_or_cls: TargetAdapterProtocol | type[Any],
    *,
    adapter_id: str | None = None,
    version: str | None = None,
    supports_idempotency: bool = True,
    supports_fencing: bool = True,
    supports_version_cas: bool = True,
    supports_status_lookup: bool = True,
    retention_horizon_seconds: int = 300,
) -> str:
    """Compute a deterministic SHA-256 hex digest of the adapter implementation + contract."""
    if isinstance(adapter_or_cls, type):
        cls = adapter_or_cls
        eff_id = adapter_id or cls.__name__
        eff_ver = version or "1.0.0"
    else:
        prof = adapter_or_cls.profile()
        return prof.code_digest

    try:
        src = inspect.getsource(cls)
    except OSError:
        src = cls.__qualname__

    payload: dict[str, Any] = {
        "class_name": cls.__qualname__,
        "source": src,
        "adapter_id": eff_id,
        "version": eff_ver,
        "supports_idempotency": supports_idempotency,
        "supports_fencing": supports_fencing,
        "supports_version_cas": supports_version_cas,
        "supports_status_lookup": supports_status_lookup,
        "retention_horizon_seconds": retention_horizon_seconds,
    }
    return canonical_request_hash(payload)
