# ADR-0002: Feature E8 — Adapter Conformance and Chaos Lab Architecture & Design Specification

*   **Author:** OpsBlaze Architecture Team
*   **Status:** Approved (Post-EGM Goldfish Review)
*   **Date:** 2026-11-07

--------------------------------------------------------------------------------

## Overview

Feature E8 establishes the Adapter Conformance and Chaos Lab for OpsBlaze (TITAN-20 Stage 3). No external mutation adapter may be enabled for consequential execution by Feature B2 (Version-Fenced Resource Reservation) or Feature B3 (Effect Receipt and Reconciliation) until it passes an automated conformance and fault-injection test suite against a disposable target simulator and its exact `(adapter_id, adapter_version, code_digest)` is durably recorded as `CONFORMANT`.

## The Problem

External infrastructure adapters often claim idempotency, fence enforcement, or status-lookup capabilities in documentation while violating those invariants under network partitions, duplicate retries, stale worker races, or target version drift. Allowing an incident recovery workflow to dispatch mutations through an unverified or upgraded adapter risks double execution, split-brain writes from zombie workers, false-success reporting when a partial failure occurred, or blind retries after idempotency key retention expires. OpsBlaze requires a deterministic, automated conformance harness and fault-injection proxy that proves adapter behavior across all 12 adversarial fault classes before granting mutation capability, and automatically revokes/quarantines the adapter when any test fails, when its code digest changes, or when its conformance attestation expires.

## Goals

*   **G1 — Typed Adapter Protocol & Capability Profile**: Define a strict, typed adapter interface supporting `apply(request)`, `query_effect(tenant_id, target_id, effect_id)`, and `read_resource(tenant_id, target_id)` alongside an immutable `AdapterCapabilityProfile` declaring idempotency support, fence enforcement, resource-version CAS support, status lookup support, effect-key retention horizon, supported fault scenarios, version, and deterministic SHA-256 `code_digest`.
*   **G2 — Disposable Target Simulator**: Provide a tenant-scoped, isolated target simulator (`DisposableTargetSimulator`) with zero external shell or production access, enforcing monotonic resource fencing (`fence >= target.highest_fence_seen`), target-version compare-and-swap (`expected_version == target.current_version`), idempotent effect-key deduplication within a configurable retention horizon, and independent resource state inspection (`read_resource`).
*   **G3 — Deterministic 12-Scenario Fault Injection Proxy**: Implement `FaultInjectionProxy` wrapping any target adapter to deterministically inject all 12 required chaos scenarios without sleeps or nondeterministic timing.
*   **G4 — Durable Conformance Registry & Mutation Capability Gate**: Persist conformance runs and capability enablement states (`READ_ONLY_UNVERIFIED`, `CONFORMANT`, `QUARANTINED`, `REVOKED_DIGEST_MISMATCH`, `EXPIRED`) in PostgreSQL (`adapter_conformance`) bound to `(adapter_id, adapter_version, code_digest)`, and enforce fail-closed verification at both B2 reservation and B3 dispatch boundaries.
*   **G5 — Structured Telemetry & Operational Runbooks**: Emit low-cardinality metrics and structured events (`adapter.conformance_pass`, `adapter.stale_fence_violation`, `adapter.duplicate_effect`, `adapter.lookup_mismatch`, `adapter.retention_gap`, `adapter.enablement_revoked`) with zero secret leakage.

## Non-Goals

*   Executing arbitrary shell commands, arbitrary HTTP requests, or unattended mutations against live production infrastructure.
*   Allowing LLM/agent outputs to register adapters, override conformance results, or bypass digest verification.
*   Building UI dashboards for chaos lab administration (out of scope for Stage 3 backend correctness).

## Critical User Journeys (CUJs)

### CUJ 1: Pre-Enablement Conformance & Chaos Suite Certification
*   **Given**: A newly registered simulator adapter (`adapter_id="sim-remediation-v1"`, `version="1.0.0"`) in default `READ_ONLY_UNVERIFIED` state.
*   **When**: The E8 `ConformanceRunner` executes the 12-scenario conformance and chaos campaign against an isolated `DisposableTargetSimulator` wrapped by `FaultInjectionProxy`.
*   **Then**: All 12 scenarios pass, the runner records a durable `CONFORMANT` attestation in PostgreSQL bound to the adapter's computed SHA-256 `code_digest` and an explicit `expires_at` timestamp, emits `adapter.conformance_pass`, and unlocks mutation capability for that exact `(adapter_id, version, code_digest)`.

### CUJ 2: Automatic Quarantine on Conformance Violation or False Success
*   **Given**: An adapter under test or runtime verification that accepts a stale fence (`fence < highest_fence_seen`) or returns `APPLIED` without mutating the target resource (`false_success`).
*   **When**: The E8 harness or B3 runtime verifier detects the contract violation.
*   **Then**: `MutationCapabilityGate.quarantine_adapter()` immediately transitions the adapter to `QUARANTINED` in a dedicated short PostgreSQL transaction (committed before raising any domain exception so outer rollbacks never lose the quarantine), records the failing scenario and evidence payload, emits `adapter.stale_fence_violation` / `adapter.enablement_revoked`, and rejects all subsequent B2 reservation and B3 mutation requests with `AdapterQuarantinedError`.

### CUJ 3: Code Digest Drift & Conformance Expiry Rejection
*   **Given**: An adapter previously certified as `CONFORMANT` with `code_digest = D1` and `expires_at = T1`.
*   **When**: The adapter implementation or version changes (`code_digest = D2 != D1`) or the current time exceeds `T1`, and a worker attempts to reserve a resource (B2) or dispatch an effect (B3).
*   **Then**: `MutationCapabilityGate.verify_mutation_allowed()` detects the digest/version mismatch or expiry, commits `status = 'REVOKED_DIGEST_MISMATCH'` or `'EXPIRED'` in a dedicated short PostgreSQL transaction, emits `adapter.enablement_revoked`, and fails closed before any external call occurs.

## Technical Plan (Architecture)

```text
+-----------------------------------------------------------------------+
|                    E8 ConformanceRunner & Chaos Lab                   |
|  12 Deterministic Scenarios -> FaultInjectionProxy -> TargetSimulator |
+-----------------------------------------------------------------------+
                                   |
                                   v (Short PostgreSQL Tx)
+-----------------------------------------------------------------------+
|             PostgreSQL: adapter_conformance (Registry)                |
|  (adapter_id, adapter_version, code_digest, status, expires_at,       |
|   retention_horizon_seconds, capabilities_json,                       |
|   conformance_evidence_json)                                          |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
|                    MutationCapabilityGate (Fail-Closed)               |
|  Enforced by B2 (ResourceReservationManager) & B3 (ActionGateway)     |
|  Requires: status == CONFORMANT, version & code_digest match,         |
|            expires_at > now, required capabilities enabled            |
+-----------------------------------------------------------------------+
```

### 1. Typed Adapter Protocol (`backend/adapters/protocol.py`)

Every target adapter implements `TargetAdapterProtocol`:
*   `profile() -> AdapterCapabilityProfile`:
    *   `adapter_id: str`
    *   `version: str`
    *   `code_digest: str` — deterministic SHA-256 hex digest computed via `canonical_request_hash` (reused from `backend/domain/incident.py`) over `inspect.getsource(cls)` + canonical capability profile fields.
    *   `supports_idempotency: bool`
    *   `supports_fencing: bool`
    *   `supports_version_cas: bool`
    *   `supports_status_lookup: bool`
    *   `retention_horizon_seconds: int` (must be `>= 60` for mutation-capable adapters)
    *   `is_simulator: bool` (must be `True` in Stage 3)
    *   `supported_actions: tuple[str, ...]`
    *   `supported_fault_scenarios: tuple[str, ...]`
*   `async apply(request: AdapterApplyRequest) -> AdapterApplyResponse`:
    *   `request`: `tenant_id: UUID`, `target_id: str`, `effect_id: str` (stable B3 idempotency key), `fence: int`, `expected_version: str`, `action_kind: str`, `action_digest: str`, `parameters: dict[str, Any]`, `correlation_id: str`.
    *   `response`: `status: AdapterApplyStatus` (`APPLIED`, `DUPLICATE_APPLIED`, `REJECTED_STALE_FENCE`, `REJECTED_VERSION_CONFLICT`, `PARTIAL_FAILURE`, `FAILED`), `external_ref: str | None`, `observed_version: str | None`, `detail: str`.
*   `async query_effect(tenant_id: UUID, target_id: str, effect_id: str) -> AdapterEffectLookupResult`:
    *   Returns `status: AdapterLookupStatus` (`COMMITTED`, `NOT_FOUND`, `PARTIAL`, `FAILED`, `RETENTION_EXPIRED`), `external_ref: str | None`, `observed_version: str | None`, `fence: int | None`, `action_digest: str | None`, `committed_at: datetime | None`.
*   `async read_resource(tenant_id: UUID, target_id: str) -> ResourceStateSnapshot`:
    *   Independent read-only inspection returning `target_id: str`, `current_version: str`, `highest_fence_seen: int`, `last_effect_id: str | None`, `state_data: dict[str, Any]`, `healthy: bool`.

### 2. Disposable Target Simulator & Strict Guard Ordering (`backend/adapters/simulator.py`)

`DisposableTargetSimulator` maintains isolated, tenant-scoped resource state in memory (`dict[tuple[UUID, str], SimulatedResource]`) with a controllable clock (`now_fn`) for deterministic retention-expiry testing.

**Post-EGM Review Guard Ordering in `DisposableTargetSimulator.apply()`**:
1.  **Positive Fence Check**: If `request.fence <= 0`, emit `adapter.stale_fence_violation` and return `REJECTED_STALE_FENCE`.
2.  **Stale Fence Guard (`fence < highest_fence_seen`)**: If `request.fence < target.highest_fence_seen`, a newer fence has already reached the target. Even if `request.effect_id` was previously committed under an older fence, an active `apply()` call with a superseded fence is a stale-worker write attempt (`query_effect` is the read-only lookup path). Emit `adapter.stale_fence_violation`, increment `adapter_stale_fence_violations_total`, and return `REJECTED_STALE_FENCE`.
3.  **Idempotency Check (`effect_id` in effect log)**:
    *   If `request.effect_id` exists in the target's effect log:
        *   If `(now - rec.committed_at).total_seconds() >= retention_horizon_seconds`, evict the expired record (or mark expired).
        *   Else if `rec.fence != request.fence` or `rec.action_digest != request.action_digest` or `rec.expected_version != request.expected_version`, return `FAILED` (conflicting payload on same `effect_id`).
        *   Else (exact retry of the committed effect under the active `highest_fence_seen == request.fence` within retention horizon): emit `adapter.duplicate_effect`, increment `adapter_duplicate_effects_total`, and return `DUPLICATE_APPLIED` with the recorded `external_ref` and `observed_version` without re-mutating the target.
4.  **Non-Idempotent Same-Fence Guard (`fence == highest_fence_seen` with a *different* `effect_id`)**:
    *   Because B2 allocates a strictly increasing fence (`latest_fence + 1`) for every new reservation, two distinct `effect_id` mutations must never execute under the same fence once a mutation has already committed at `highest_fence_seen`. If `request.fence == target.highest_fence_seen` and `target.last_effect_id is not None` and `target.last_effect_id != request.effect_id`, emit `adapter.stale_fence_violation` and return `REJECTED_STALE_FENCE`.
5.  **Target Version CAS Guard (`expected_version != target.current_version`)**:
    *   Update `target.highest_fence_seen = max(target.highest_fence_seen, request.fence)` (recording that fence `request.fence` was presented) and return `REJECTED_VERSION_CONFLICT` with `observed_version=target.current_version`.
6.  **Atomic State Mutation**:
    *   Advance `target.highest_fence_seen = request.fence`, compute the next deterministic version (`v{n+1}`), apply the typed state change (`restart_service`, `drain_traffic`, `scale_workers`, `rollback_config`), set `target.last_effect_id = request.effect_id`, `target.healthy = True`, record the receipt in the target's retention-bounded effect log, and return `APPLIED`.

### 3. Deterministic 12-Scenario Fault Injection Proxy (`backend/adapters/fault_proxy.py`)

`FaultInjectionProxy` wraps a `TargetAdapterProtocol` and supports deterministic fault rules:
1.  **`DUPLICATE_APPLY`**: Invokes underlying `apply` twice with the same `effect_id` and verifies only one state transition occurs (`v1 -> v2`, not `v3`) and `adapter.duplicate_effect` is emitted.
2.  **`LOST_RESPONSE_AFTER_COMMIT`**: Commits the mutation in the underlying simulator (`v1 -> v2`), then raises `AdapterTransportTimeoutError("Simulated lost response after commit")` so B3 transitions `SENT -> UNKNOWN` and reconciles via `query_effect`.
3.  **`STALE_FENCE`**: Sends an `apply` with `fence < highest_fence_seen` and verifies `REJECTED_STALE_FENCE` and `adapter.stale_fence_violation`.
4.  **`COMPETING_FENCES`**: Drives two competing `apply` requests with `fence=n` and `fence=n+1` where `n+1` lands first, proving `fence=n` is deterministically rejected with `REJECTED_STALE_FENCE`.
5.  **`EXTERNAL_VERSION_CHANGE`**: Advances the underlying resource version out-of-band (`v1 -> v2`) before `apply(expected_version="v1")` and verifies `REJECTED_VERSION_CONFLICT`.
6.  **`PARTIAL_EFFECT`**: Marks the target in a dirty partial state (`healthy=False`, `state_data["partial_corruption"]=True`), records a `PARTIAL` effect entry, and returns `PARTIAL_FAILURE` (or raises timeout with `query_effect` returning `PARTIAL`) so B3 transitions to `NEEDS_REVIEW`.
7.  **`FALSE_SUCCESS_RESPONSE`**: Intercepts `apply` and returns `AdapterApplyResponse(status=APPLIED, observed_version="v2", external_ref="fake-ref")` WITHOUT mutating the underlying simulator (which stays at `"v1"`), so B3's independent postcondition verifier (`read_resource`) catches the discrepancy (`verifier.false_success` + `adapter.lookup_mismatch`), quarantines the adapter, and transitions the receipt to `NEEDS_REVIEW`.
8.  **`STATUS_LOOKUP_FAILURE`**: Causes `query_effect` to raise transient `AdapterLookupError` for a configured number of attempts before succeeding (or exhausting reconciliation attempts).
9.  **`STATUS_STORE_OUTAGE`**: Causes `query_effect` to fail continuously with `AdapterUnavailableError` while `read_resource` remains independently queryable.
10. **`RETENTION_EXPIRY`**: Advances the simulator clock past `retention_horizon_seconds` after an ambiguous `UNKNOWN` effect, causing `query_effect` to return `RETENTION_EXPIRED` and emitting `adapter.retention_gap`.
11. **`ADAPTER_VERSION_CHANGE`**: Overrides the adapter's reported `version` or `code_digest` mid-lifecycle without re-running conformance, proving `MutationCapabilityGate` immediately revokes mutation rights (`REVOKED_DIGEST_MISMATCH`) and emits `adapter.enablement_revoked`.
12. **`CONFORMANCE_INVALIDATION`**: Wraps a defective adapter implementation (e.g., accepting stale fences or ignoring version CAS) during `ConformanceRunner.run_suite()`, proving the runner marks the adapter `QUARANTINED` in PostgreSQL and blocks all B2/B3 usage.

### 4. PostgreSQL Schema Extension (`adapter_conformance` in `0002_stage3_b2_b3_e8.py`)

In revision `0002_stage3_b2_b3_e8` (`down_revision = "b3_005_claim_payload"`):
*   Create table `adapter_conformance`:
    *   `adapter_id VARCHAR NOT NULL PRIMARY KEY`
    *   `adapter_version VARCHAR NOT NULL`
    *   `code_digest VARCHAR NOT NULL`
    *   `status VARCHAR NOT NULL`
    *   `retention_horizon_seconds INTEGER NOT NULL`
    *   `capabilities_json JSON NOT NULL`
    *   `conformance_evidence_json JSON NOT NULL`
    *   `verified_at TIMESTAMPTZ NULL`
    *   `expires_at TIMESTAMPTZ NULL`
    *   `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
    *   `CheckConstraint("status IN ('READ_ONLY_UNVERIFIED', 'CONFORMANT', 'QUARANTINED', 'REVOKED_DIGEST_MISMATCH', 'EXPIRED')", name="chk_adapter_conformance_status_valid")`
    *   `CheckConstraint("retention_horizon_seconds >= 0", name="chk_adapter_retention_nonnegative")`

### 5. EGM Review Resolution Summary
*   **Resolved `[CRITICAL-3]` (Guard ordering in `DisposableTargetSimulator`)**: Placed the stale fence guard (`request.fence < target.highest_fence_seen`) *before* the idempotency check in `apply()`, and added a same-fence distinct-`effect_id` rejection guard (`request.fence == target.highest_fence_seen` with different `effect_id`), ensuring zombie workers cannot mask stale writes as `DUPLICATE_APPLIED`.
*   **Resolved `[IMPORTANT-2]` (Dedicated transaction for quarantine/revocation)**: `MutationCapabilityGate` commits `QUARANTINED`, `REVOKED_DIGEST_MISMATCH`, and `EXPIRED` status updates in a dedicated short PostgreSQL transaction before raising domain exceptions so outer rollbacks never erase the revocation.
*   **Resolved `[MINOR]` (`canonical_request_hash` reuse & check constraints)**: Reused `canonical_request_hash` from `backend/domain/incident.py` for `code_digest` and added `chk_adapter_conformance_status_valid` and `chk_adapter_retention_nonnegative`.
