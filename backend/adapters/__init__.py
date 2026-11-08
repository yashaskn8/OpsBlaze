"""OpsBlaze Feature E8 — Adapter Conformance and Chaos Lab."""

from __future__ import annotations

from backend.adapters.conformance import (
    AdapterConformanceError,
    AdapterDigestMismatchError,
    AdapterEnablementExpiredError,
    AdapterNotConformantError,
    AdapterQuarantinedError,
    ConformanceReport,
    ConformanceRunner,
    ConformanceStatus,
    MutationCapabilityGate,
)
from backend.adapters.fault_proxy import (
    AdapterLookupError,
    AdapterTransportTimeoutError,
    AdapterUnavailableError,
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.protocol import (
    AdapterApplyRequest,
    AdapterApplyResponse,
    AdapterApplyStatus,
    AdapterCapabilityProfile,
    AdapterEffectLookupResult,
    AdapterLookupStatus,
    ResourceStateSnapshot,
    TargetAdapterProtocol,
    compute_adapter_code_digest,
)
from backend.adapters.simulator import DisposableTargetSimulator

__all__ = [
    "AdapterApplyRequest",
    "AdapterApplyResponse",
    "AdapterApplyStatus",
    "AdapterCapabilityProfile",
    "AdapterConformanceError",
    "AdapterDigestMismatchError",
    "AdapterEffectLookupResult",
    "AdapterEnablementExpiredError",
    "AdapterLookupError",
    "AdapterLookupStatus",
    "AdapterNotConformantError",
    "AdapterQuarantinedError",
    "AdapterTransportTimeoutError",
    "AdapterUnavailableError",
    "ConformanceReport",
    "ConformanceRunner",
    "ConformanceStatus",
    "DisposableTargetSimulator",
    "FaultInjectionProxy",
    "FaultRule",
    "FaultScenario",
    "MutationCapabilityGate",
    "ResourceStateSnapshot",
    "TargetAdapterProtocol",
    "compute_adapter_code_digest",
]
