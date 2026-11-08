# Architecture-to-Implementation Drift Report — TITAN-20 Stage 3 (B1 + B2 + B3 + E8)

* **Date:** 2026-11-07
* **Specification References:**
  * [`docs/adr/0001-b1-durable-incident-workflow.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md)
  * [`docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md)
  * [`docs/adr/0003-b2-version-fenced-resource-reservation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md)
  * [`docs/adr/0004-b3-effect-receipt-and-reconciliation.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md)
* **Scope:** OpsBlaze Features B1 (Durable Incident Workflow), B2 (Version-Fenced Resource Reservation), B3 (Effect Receipt and Reconciliation), and E8 (Adapter Conformance and Chaos Lab).
* **Review Methodology:** Elephant-Goldfish Model (`egm-drift-report` + 3 Goldfish Subagents: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`) + Independent 5-Axis Code & Architecture Review (`ab4b2296-02a1-4c2f-a6f4-1d403492e3d5`).

---

## 1. Executive Summary

We audited the implemented codebase, Alembic migrations, state machines, Temporal workflows, and test suite against `ADR-0001` (B1), `ADR-0002` (E8), `ADR-0003` (B2), and `ADR-0004` (B3). All architectural goals, Goldfish review rulings (`[CRITICAL-1]`, `[CRITICAL-2]`, `[CRITICAL-3]`, `[IMPORTANT-1]` through `[IMPORTANT-4]`), and security boundaries are implemented with **zero silent or unsafe architectural drift**.

---

## 2. Specification vs. Implementation Comparison Matrix

### 2.1 Feature B1 — Durable Incident Workflow (`ADR-0001`)

| Spec Section / Component | Specified Design (`ADR-0001`) | Implemented Code | Drift Classification | Verification Evidence |
|---|---|---|---|---|
| **PostgreSQL Schema & Migration Recovery** | Reconstruct `b3_005_claim_payload` with idempotent DDL (`IF NOT EXISTS`) for all 6 foundation tables (`tenant`, `incident`, `outbox`, `resource_epoch`, `resource_reservation`, `effect_receipt`). | `backend/persistence/models.py`, `backend/persistence/alembic/versions/0001_b3_005_claim_payload.py` | **None (Exact Match)** | `test_isolated_schema_alembic_upgrade_and_downgrade` passes (`b3_005_claim_payload` <-> `stage3_002_b2_b3_e8`). |
| **Domain State Machine & Canonical Hashing** | 10-state `IncidentStatus` enum, `ALLOWED_TRANSITIONS`, `CANCELLABLE_STATUSES`, `TERMINAL_STATUSES`, NFC string normalization, null-vs-missing optional field normalization, sorted-key SHA-256 digest. | `backend/domain/incident.py` (`IncidentStatus`, `canonical_request_hash`, `validate_transition`) | **None (Exact Match)** | `test_b1_state_machine.py` (10/10 pass) & `test_b1_api_idempotency.py` (9/9 pass). |
| **Atomic Admission & Outbox Repository** | Single short transaction inserting `incident` + `outbox`; explicit `session.rollback()` on `ux_incident_key` `IntegrityError` race followed by fresh read and `request_sha256` comparison. | `backend/persistence/repositories.py` (`IncidentRepository.admit_incident`, `OutboxRepository`) | **None (Exact Match)** | `test_b1_api_idempotency.py`, `test_b1_db_failures.py`. |
| **Outbox Dispatcher & Crash Recovery** | Short claim transaction (`FOR UPDATE SKIP LOCKED`), lease via `available_at`, external Temporal `start_workflow` outside DB transaction, idempotent `WorkflowAlreadyStartedError` handling, short ack transaction transitioning `ACCEPTED -> DISPATCH_PENDING`. | `backend/workflows/dispatcher.py` (`OutboxDispatcher`, `TemporalWorkflowStarter`) | **None (Exact Match)** | `test_b1_dispatcher_crash.py` (crash windows 1–10). |

### 2.2 Feature E8 — Adapter Conformance and Chaos Lab (`ADR-0002`)

| Spec Section / Component | Specified Design (`ADR-0002`) | Implemented Code | Drift Classification | Verification Evidence |
|---|---|---|---|---|
| **Typed Adapter Contract & Code Digest** | `TargetAdapterProtocol` with `profile()`, `apply()`, `query_effect()`, `read_resource()`; deterministic `compute_adapter_code_digest()` reusing `canonical_request_hash()`. | `backend/adapters/protocol.py` | **None (Exact Match)** | `test_e8_conformance.py::test_e8_simulator_protocol_idempotency_fencing_and_version_cas`. |
| **Disposable Simulator Guard Ordering (`[CRITICAL-3]`)** | Strict 6-step guard order in `DisposableTargetSimulator.apply()`: (1) `fence <= 0` reject, (2) `fence < highest_fence_seen` reject before idempotency check, (3) idempotent replay within `retention_horizon_seconds`, (4) non-idempotent same-fence distinct-`effect_id` reject, (5) `expected_version != current_version` CAS reject, (6) atomic version advance & state mutation. | `backend/adapters/simulator.py` (`DisposableTargetSimulator.apply`) | **None (Exact Match)** | `test_e8_conformance.py::test_e8_simulator_protocol_idempotency_fencing_and_version_cas` & `test_b2_fencing.py`. |
| **Deterministic 12-Scenario Fault Proxy** | `FaultInjectionProxy` intercepting `TargetAdapterProtocol` to deterministically inject all 12 required fault scenarios (`DUPLICATE_APPLY`, `LOST_RESPONSE_AFTER_COMMIT`, `STALE_FENCE`, `COMPETING_FENCES`, `EXTERNAL_VERSION_CHANGE`, `PARTIAL_EFFECT`, `FALSE_SUCCESS_RESPONSE`, `STATUS_LOOKUP_FAILURE`, `STATUS_STORE_OUTAGE`, `RETENTION_EXPIRY`, `ADAPTER_VERSION_CHANGE`, `DEFECTIVE_ADAPTER_QUARANTINE`). | `backend/adapters/fault_proxy.py` (`FaultInjectionProxy`, `FaultScenario`, `FaultRule`) | **None (Exact Match)** | `test_e8_conformance.py::test_e8_all_12_fault_injection_scenarios`. |
| **Conformance Runner & Fail-Closed Capability Gate** | `ConformanceRunner` executing all 12 scenarios + persisting `adapter_conformance`; `MutationCapabilityGate` verifying `status == CONFORMANT`, `code_digest` match, and `expires_at > now()`, committing `QUARANTINED` / `REVOKED_DIGEST_MISMATCH` / `EXPIRED` in a dedicated transaction before raising. | `backend/adapters/conformance.py` (`ConformanceRunner`, `MutationCapabilityGate`) | **None (Exact Match)** | `test_e8_conformance.py::test_e8_conformance_runner_gate_quarantine_digest_and_expiry`. |

### 2.3 Feature B2 — Version-Fenced Resource Reservation (`ADR-0003`)

| Spec Section / Component | Specified Design (`ADR-0003`) | Implemented Code | Drift Classification | Verification Evidence |
|---|---|---|---|---|
| **Monotonic Fence Allocation (`resource_epoch`)** | Upsert `(tenant_id, target_id)` into `resource_epoch`, lock row with `SELECT ... FOR UPDATE`, sync with `live_snapshot.highest_fence_seen`, allocate `next_fence = latest_fence + 1`. Never reset or reuse fences across release/cancel/expire. | `backend/effects/fencing.py` (`ResourceReservationManager.acquire_reservation`) | **None (Exact Match)** | `test_b2_fencing.py` & `test_stage3_b2_b3_races.py`. |
| **Active Lease Partial Unique Index & Unresolved B3 Receipt Guard (`[CRITICAL-1]`)** | `ux_active_reservation_per_target` on `(tenant_id, target_id) WHERE state IN ('RESERVED', 'DISPATCHED')`. Before expiring an overdue `DISPATCHED` reservation, check `effect_receipt` for `state IN ('SENT', 'UNKNOWN', 'CONFIRMED')` at the same fence; if present, block takeover with `ResourceUncertainEffectPendingError`. | `backend/persistence/models.py`, `0002_stage3_b2_b3_e8.py`, `backend/effects/fencing.py` | **None (Exact Match)** | `test_stage3_b2_b3_races.py::test_b2_b3_unresolved_dispatched_receipt_blocks_competing_reservation_even_past_lease`. |
| **Fail-Closed P2 Approval Validator** | `ApprovedPlanContext` + `verify_approved_plan_context()` denying `None`, expired approvals, non-simulator modes, and forbidden model/agent roles (`agent`, `model`, `investigator`, `llm`, `autonomous_agent`), and verifying exact match on `tenant_id`, `incident_id`, `target_id`, `plan_digest`, `action_digest`, and `expected_version`. | `backend/effects/fencing.py` (`ApprovedPlanContext`, `verify_approved_plan_context`) | **None (Exact Match)** | `test_b2_fencing.py::test_b2_reservation_state_machine_and_approval_validator` & `test_stage3_b1_b2_b3_e8_integration.py`. |

### 2.4 Feature B3 — Effect Receipt and Reconciliation (`ADR-0004`)

| Spec Section / Component | Specified Design (`ADR-0004`) | Implemented Code | Drift Classification | Verification Evidence |
|---|---|---|---|---|
| **Atomic Pre-Dispatch Transaction (`RESERVED -> DISPATCHED` + `PREPARED -> SENT`)** | Single short transaction verifying reservation lease/fence/version, transitioning reservation `RESERVED -> DISPATCHED`, inserting `effect_receipt` (`PREPARED`), and transitioning to `SENT` with `claim_token` and `lease_expires_at` before `adapter.apply()`. | `backend/effects/receipts.py` (`ActionGateway.execute_with_permit`) | **None (Exact Match)** | `test_b3_effect_state_machine.py` & `test_stage3_b1_b2_b3_e8_integration.py`. |
| **`claim_token` CAS & Expired `SENT` Sweep (`[CRITICAL-2]`)** | `EffectReconciler.reconcile_pending()` claims both `UNKNOWN` and lease-expired `SENT` rows via `FOR UPDATE SKIP LOCKED`, rotating `claim_token`; every post-I/O state transition checks `row.claim_token == claim_token` under `FOR UPDATE` and raises `StaleEffectClaimError` on mismatch. | `backend/effects/receipts.py` (`ActionGateway`, `EffectReconciler`) | **None (Exact Match)** | `test_stage3_b2_b3_races.py::test_b3_concurrent_reconcilers_skip_locked_and_expired_sent_sweep`. |
| **Independent Postcondition Verification (`CONFIRMED != VERIFIED`)** | `IndependentPostconditionVerifier` calls `adapter.read_resource()` and verifies `healthy`, `current_version == observed_version`, `current_version != expected_version`, `highest_fence_seen >= fence`, and `last_effect_id == idempotency_key`. On false success, quarantines adapter in dedicated tx and marks receipt `NEEDS_REVIEW`. | `backend/effects/receipts.py` (`IndependentPostconditionVerifier`, `ActionGateway.verify_and_finalize_confirmed_receipt`) | **None (Exact Match)** | `test_b3_effect_state_machine.py::test_b3_false_success_partial_effect_retention_expiry_and_lookup_outage`. |
| **Temporal B1 + B2 + B3 + E8 Workflow Integration** | `IncidentWorkflow` supports `approve_and_execute` signal gated behind `workflow.patched("stage3-b2-b3-execution-v1")`, driving `AWAITING_APPROVAL -> EXECUTING -> VERIFYING -> VERIFIED` (or `NEEDS_REVIEW` / `FAILED`) via deterministic activity calls. | `backend/workflows/incident_workflow.py`, `backend/workflows/activities.py`, `backend/workflows/worker.py` | **None (Exact Match)** | `test_stage3_b1_b2_b3_e8_integration.py` (2/2 pass) & `test_b1_temporal_workflow.py` (4/4 pass). |

---

## 3. Scope & Non-Goal Compliance Verification

* **No Unapproved Features (`A1`, `A2`, `P1`, `P2`, etc.):** Confirmed. Only `B2`, `B3`, and `E8` (integrated with `B1`) were implemented; P2 is represented strictly by the minimal fail-closed `ApprovedPlanContext` validator.
* **No Unapproved Infrastructure:** Confirmed. Uses only PostgreSQL 17.11, Temporal (`temporalio`), FastAPI, SQLAlchemy 2.0 async, Alembic, and React 19 + TypeScript.
* **Database Non-Destructiveness:** Confirmed. Pre-migration backups `.local/backups/opsblaze_pre_stage3.dump` and `.local/backups/opsblaze_test_pre_stage3.dump` were created and verified via `pg_restore --list`. All 22 tenants, 22 incidents, and 29 effect receipts in `opsblaze_test` and 0 rows in `opsblaze` are 100% preserved.

---

## 4. Stage 4 — Durable Integrity, Disaster Recovery & Cross-Store Consistency Drift Audit (`ADR-0005`)

* **Specification Reference:** [`docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-stage4-durable-integrity-and-cross-store-recovery.md)
* **Goldfish Review Rulings:** Incorporates all rulings from 3 independent Goldfish subagents (`egm-design`: `e2c80f6b-5095-4b69-857d-7f96b2ff7cdb`, `egm-implementation-readiness`: `5bc3fbc5-fd3f-4387-b979-f52cd1c3b473`, `egm-zoom-out`: `96a5f955-42db-45b9-9ab3-04e1bb856f10`).

### 4.1 Specification vs. Implementation Comparison Matrix

| Spec Section / Component | Specified Design (`ADR-0005`) | Implemented Code | Drift Classification | Verification Evidence |
|---|---|---|---|---|
| **Degraded Recovery Gate & Scope Enforcement** | Fail-closed `DegradedRecoveryGate` pausing mutations and dispatcher during recovery; read-only operations continue. | `backend/domain/incident.py`, `backend/workflows/recovery.py`, `backend/effects/fencing.py`, `backend/effects/receipts.py`, `backend/workflows/dispatcher.py` | **None (Exact Match)** | `test_scenario_a_postgres_behind_temporal`, `test_divergent_epoch_fences_db_behind_and_target_ahead`. |
| **Monotonic Forward Status Projection** | `IncidentRepository.reconcile_status_projection_forward()` advancing status forward monotonically without regressing; marks outbox delivered atomically. | `backend/persistence/repositories.py` | **None (Exact Match)** | `tests/integration/test_stage4_cross_store_recovery.py` (3/3 pass). |
| **4-Source Fence Watermark Aggregation** | Fence watermark computed as `max(epoch.latest_fence, target.highest_fence_seen, max(reservations), max(receipts))`, preventing stale fence reuse after DB restore. | `backend/effects/fencing.py`, `backend/workflows/recovery.py` | **None (Exact Match)** | `test_scenario_c_divergent_restores_and_target_ahead` & `test_divergent_epoch_fences_db_behind_and_target_ahead`. |
| **Legacy Effect Receipt Quarantine (`NOT_FOUND -> NEEDS_REVIEW`)** | Historical receipts (29 rows in `opsblaze_test`) lacking `claim_token` or with empty `adapter_digest` route to `NEEDS_REVIEW` on target `NOT_FOUND` (never `FAILED` or re-dispatched). | `backend/effects/receipts.py` (`reconcile_pending`), `backend/workflows/recovery.py` | **None (Exact Match)** | `tests/unit/test_stage4_historical_receipt_safety.py` (2/2 pass). |
| **Expanded `ALLOWED_EFFECT_TRANSITIONS`** | `ALLOWED_EFFECT_TRANSITIONS[PREPARED]` allows `NEEDS_REVIEW` for orphaned pre-dispatch receipts older than 60s without active leases. | `backend/effects/receipts.py` | **None (Exact Match)** | `tests/unit/test_stage4_historical_receipt_safety.py`. |
| **Workflow Determinism & Replay Compatibility** | Replay of 6 canonical histories with Temporal `Replayer`; detects adversarial non-determinism with failure. | `backend/workflows/incident_workflow.py`, `fixtures/workflow_histories/`, `tests/replay/test_stage4_workflow_history_replay.py` | **None (Exact Match)** | `tests/replay/test_stage4_workflow_history_replay.py` (7/7 pass). |
| **Concurrency Chaos & State Loss Drills** | 10 concurrent incidents recovering simultaneously, simulator state loss detection, 0 deadlocks, 0 invalid transitions. | `tests/chaos/test_stage4_adversarial_chaos_campaign.py` | **None (Exact Match)** | `tests/chaos/test_stage4_adversarial_chaos_campaign.py` (4/4 pass). |

---

## 5. Architectural Invariant Verdict: ZERO DRIFT
The Stage 4 implementation rigorously satisfies all invariants defined in root `AGENTS.md`, TITAN-20 Blueprint, and `ADR-0005`. No architectural divergence, no silent regressions, and no unauthorized features were introduced.

