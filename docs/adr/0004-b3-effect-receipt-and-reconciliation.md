# ADR-0004: Feature B3 — Effect Receipt and Reconciliation Architecture & Design Specification

*   **Author:** OpsBlaze Architecture Team
*   **Status:** Approved (Post-EGM Goldfish Review)
*   **Date:** 2026-11-07

--------------------------------------------------------------------------------

## Overview

Feature B3 implements Effect Receipt and Reconciliation for OpsBlaze (TITAN-20 Stage 3). Every consequential external action is durably recorded in PostgreSQL (`effect_receipt`) with a deterministic idempotency key before any external adapter call is made. When a timeout, transport error, or worker crash leaves an external mutation outcome ambiguous, B3 explicitly records the receipt state as `UNKNOWN`, prohibits blind re-execution, reconciles the outcome via the E8 adapter's `query_effect(tenant_id, target_id, effect_id)` status lookup, and requires an independent postcondition verification (`read_resource(tenant_id, target_id)`) before transitioning any effect to `VERIFIED`.

## The Problem

External mutations across network boundaries cannot participate in a PostgreSQL transaction. When a worker invokes `adapter.apply(...)` and experiences a socket timeout, connection reset, or process crash, the worker does not know whether the target system committed the mutation, rejected it, partially applied it, or never received it. A naive retry policy that blindly re-sends `apply` on timeout will cause duplicate mutations if the first call actually succeeded or if the adapter's idempotency window expired. Conversely, trusting an adapter's `APPLIED` response without independent postcondition verification allows a faulty adapter (`FALSE_SUCCESS_RESPONSE`) to mark a broken incident as `VERIFIED`.

## Goals

*   **G1 — Atomic Pre-Dispatch Intent (`PREPARED -> SENT` + B2 `RESERVED -> DISPATCHED`)**: Persist an `effect_receipt` row in state `PREPARED` and claim it into `SENT` (with a unique `claim_token`, `lease_owner`, `lease_expires_at`, and `adapter_digest`) atomically in the **same** short PostgreSQL transaction that transitions the B2 `resource_reservation` from `RESERVED` to `DISPATCHED`. Never hold a database transaction open during `apply`, `query_effect`, or `read_resource`.
*   **G2 — Deterministic Effect Identity**: Derive `idempotency_key` deterministically via `canonical_request_hash` (reused from `backend/domain/incident.py`) over `(tenant_id, incident_id, plan_digest, target_id, adapter_id, action_digest, expected_version)` and enforce `ux_effect_tenant_idempotency UNIQUE (tenant_id, idempotency_key)`.
*   **G3 — Explicit `UNKNOWN` State, Expired `SENT` Sweep, & Prohibition of Blind Retries**:
    *   Enforce the B3 state machine:
        *   `PREPARED -> SENT -> CONFIRMED -> VERIFIED`
        *   `PREPARED -> FAILED` (if pre-dispatch reservation/capability/version recheck fails before sending)
        *   `SENT -> UNKNOWN | CONFIRMED | FAILED | NEEDS_REVIEW`
        *   `UNKNOWN -> CONFIRMED | FAILED | NEEDS_REVIEW` (or controlled `UNKNOWN -> SENT` retry *only* when `query_effect` authoritatively returns `NOT_FOUND` within the active retention horizon and all B2/E8/version guards pass)
        *   `CONFIRMED -> VERIFIED | FAILED | NEEDS_REVIEW` (via independent postcondition verifier)
    *   Every post-I/O state transition in `ActionGateway` and `EffectReconciler` enforces `claim_token` compare-and-swap (`CAS`) so a delayed worker whose lease expired can never overwrite a reconciler's state transition.
    *   When a worker crashes while a receipt is in `SENT` and `lease_expires_at <= now()`, `EffectReconciler` claims the expired `SENT` row using `FOR UPDATE SKIP LOCKED` and atomically transitions `SENT -> UNKNOWN` with a fresh `claim_token` before invoking `query_effect`.
*   **G4 — Independent Postcondition Verification (`CONFIRMED -> VERIFIED`)**: After an effect reaches `CONFIRMED` (either from `apply` returning `APPLIED`/`DUPLICATE_APPLIED` or via `UNKNOWN` reconciliation finding `COMMITTED`), B3 invokes `IndependentPostconditionVerifier.verify()`, which calls `adapter.read_resource(tenant_id, target_id)` and checks that:
    1.  `snapshot.current_version == receipt.observed_version` and `snapshot.current_version != receipt.expected_version`,
    2.  `snapshot.highest_fence_seen >= receipt.fence`,
    3.  `snapshot.last_effect_id == receipt.idempotency_key`, and
    4.  `snapshot.healthy is True` and expected action postconditions hold.
    If postcondition verification fails despite the adapter claiming `APPLIED`/`COMMITTED`, B3 emits `verifier.false_success` and `adapter.lookup_mismatch`, quarantines the adapter in E8 (`MutationCapabilityGate.quarantine_adapter`), transitions the receipt `CONFIRMED -> NEEDS_REVIEW`, and releases the B2 reservation.
*   **G5 — Minimal Fail-Closed Future P2 Approval Dependency**: Define a frozen `ApprovedPlanContext` model and `verify_approved_plan_context()` validator that **fails closed** by default (`ApprovalRequiredError`) and only permits execution when `execution_mode == "SIMULATOR_APPROVED"`, `approved_by_role` is an authorized human/simulator role (explicitly rejecting `"agent"`, `"model"`, `"investigator"`, `"llm"`), `expires_at > now`, the target adapter has `is_simulator is True`, and `(tenant_id, incident_id, target_id, plan_digest, action_digest, expected_version)` match identically.

## Non-Goals

*   Implementing the P2 human approval UI or REST endpoints.
*   Allowing unattended production mutations or bypassing B2 fencing or E8 conformance checks.

## Critical User Journeys (CUJs)

### CUJ 1: Normal Happy-Path Execution with Independent Verification
*   **Given**: An incident in `AWAITING_APPROVAL` with a valid simulator `ApprovedPlanContext`, a `CONFORMANT` E8 adapter, and an active B2 `MutationPermit(fence=1, expected_version="v1")`.
*   **When**: `ActionGateway.execute_with_permit()` runs.
*   **Then**:
    1.  **Short Tx 1 (Pre-Dispatch Claim)**: Verifies E8 conformance (`CONFORMANT` + `code_digest`), verifies B2 reservation `RESERVED(fence=1, expires_at > now)` and `resource_epoch.latest_fence == 1`, transitions reservation `RESERVED -> DISPATCHED` (extending `expires_at` for execution), and creates/transitions `effect_receipt` `PREPARED -> SENT(attempt_count=1, claim_token=C1, adapter_digest=code_digest)`.
    2.  **External Call 1 (No DB Tx)**: Calls `adapter.apply(...)`, receiving `APPLIED(observed_version="v2", external_ref="sim-ref-1")`.
    3.  **Short Tx 2 (Confirm)**: Verifies `claim_token == C1` and transitions receipt `SENT -> CONFIRMED(observed_version="v2", external_ref="sim-ref-1")`.
    4.  **External Call 2 (No DB Tx)**: `IndependentPostconditionVerifier` calls `adapter.read_resource(...)` and confirms `current_version == "v2"`, `highest_fence_seen >= 1`, `last_effect_id == idempotency_key`, and `healthy is True`.
    5.  **Short Tx 3 (Verify & Release)**: Verifies `claim_token == C1`, transitions receipt `CONFIRMED -> VERIFIED`, transitions B2 reservation `DISPATCHED -> RELEASED`, and updates `resource_epoch.version_seen = "v2"` (if `resource_epoch.latest_fence == receipt.fence`).

### CUJ 2: Lost Response After Commit -> `UNKNOWN` -> Reconciliation to `VERIFIED`
*   **Given**: `FaultInjectionProxy` is configured with `LOST_RESPONSE_AFTER_COMMIT` for `effect_id`.
*   **When**: `ActionGateway` calls `adapter.apply(...)`, the simulator commits `v1 -> v2`, and the proxy raises `AdapterTransportTimeoutError`.
*   **Then**:
    1.  `ActionGateway` catches the transport error and commits `SENT -> UNKNOWN` in a short transaction (verifying `claim_token == C1`, clearing `lease_expires_at` so the reconciler can claim it immediately, and extending the B2 `DISPATCHED` reservation lease by the reconciliation window), emitting `effect.unknown_age`.
    2.  `EffectReconciler.reconcile_pending()` claims the `UNKNOWN` receipt using `FOR UPDATE SKIP LOCKED` over `ix_effect_receipt_pending_recon` with a new `claim_token = C2`, emits `reconciliation.attempt`, and calls `adapter.query_effect(tenant_id, target_id, effect_id)` outside the DB transaction.
    3.  `query_effect` returns `COMMITTED(observed_version="v2", external_ref="sim-ref-1")`.
    4.  The reconciler transitions `UNKNOWN -> CONFIRMED`, runs `IndependentPostconditionVerifier` (`read_resource`), transitions `CONFIRMED -> VERIFIED`, and releases the B2 reservation. Zero duplicate `apply` calls are made.

### CUJ 3: False Success Detection & Automatic Adapter Quarantine
*   **Given**: `FaultInjectionProxy` is configured with `FALSE_SUCCESS_RESPONSE` (returns `APPLIED` with `observed_version="v2"` without mutating the target, which remains at `"v1"`).
*   **When**: `ActionGateway` transitions `SENT -> CONFIRMED` and runs `IndependentPostconditionVerifier`.
*   **Then**: `read_resource` returns `current_version="v1" != "v2"`. The verifier emits `verifier.false_success` and `adapter.lookup_mismatch`, quarantines the adapter in `adapter_conformance` (in a dedicated transaction), transitions the receipt `CONFIRMED -> NEEDS_REVIEW`, releases the B2 reservation without advancing `resource_epoch.version_seen`, and transitions the B1 incident to `NEEDS_REVIEW`.

### CUJ 4: Deterministic `UNKNOWN` Reconciliation Decision Table
When `EffectReconciler` claims an `UNKNOWN` (or lease-expired `SENT -> UNKNOWN`) receipt and calls `adapter.query_effect(tenant_id, target_id, effect_id)`:
1.  ** Dual-Layer Retention Horizon Check First**:
    *   Compute `age_sec = (now - receipt.created_at).total_seconds()`.
    *   If `age_sec >= adapter.profile().retention_horizon_seconds` OR `lookup.status == AdapterLookupStatus.RETENTION_EXPIRED`:
        *   Emit `adapter.retention_gap`, transition `UNKNOWN -> NEEDS_REVIEW`, release B2 reservation (`DISPATCHED -> RELEASED`), and **never** re-run `apply`.
2.  **`AdapterLookupStatus.COMMITTED`**:
    *   Transition `UNKNOWN -> CONFIRMED(observed_version=lookup.observed_version, external_ref=lookup.external_ref)`, run `IndependentPostconditionVerifier` (`read_resource`), transition `CONFIRMED -> VERIFIED` (or `NEEDS_REVIEW` if postconditions fail), and release B2 reservation (updating `resource_epoch.version_seen = lookup.observed_version` if `resource_epoch.latest_fence == receipt.fence`).
3.  **`AdapterLookupStatus.PARTIAL`**:
    *   Transition `UNKNOWN -> NEEDS_REVIEW` and release B2 reservation.
4.  **`AdapterLookupStatus.FAILED`**:
    *   Transition `UNKNOWN -> FAILED` and release B2 reservation.
5.  **`AdapterLookupStatus.NOT_FOUND` (within active retention horizon)**:
    *   Because the effect is still within `retention_horizon_seconds` and the adapter authoritatively reports `NOT_FOUND` (the request never committed at the target):
        *   If `allow_reapply_on_not_found is True` (default `True` when `receipt.attempt_count < max_apply_attempts`, default `2`), and B2 reservation is still active (`state == 'DISPATCHED'`, `expires_at > now`, `fence == resource_epoch.latest_fence`), and E8 adapter is still `CONFORMANT`, and `read_resource().current_version == receipt.expected_version`: transition `UNKNOWN -> SENT(attempt_count = attempt_count + 1)` and execute a single controlled idempotent re-apply using the stored `action_payload`.
        *   Otherwise (retry budget reached or reservation/version no longer valid): transition `UNKNOWN -> FAILED` and release B2 reservation.
6.  **`AdapterLookupError` / `AdapterUnavailableError` (Scenarios 8 & 9: Lookup Failure / Status Store Outage)**:
    *   Increment `receipt.attempt_count`, record `last_error`, emit `reconciliation.attempt`.
    *   If `receipt.attempt_count < max_reconcile_attempts` (default `3`), keep receipt in `UNKNOWN` with backoff on `lease_expires_at`.
    *   When `receipt.attempt_count >= max_reconcile_attempts`, transition `UNKNOWN -> NEEDS_REVIEW` and release B2 reservation so the incident is never stuck indefinitely.

## Temporal `IncidentWorkflow` & Activity Integration Contract

To integrate `B1 + B2 + B3 + E8` without breaking any of the 43 existing B1 tests or archived B1 replay histories:
1.  **Backward-Compatible Signals on `IncidentWorkflow` (`backend/workflows/incident_workflow.py`)**:
    *   Existing `@workflow.signal(name="request_cancel")` and `@workflow.signal(name="resolve_awaiting_approval")` remain 100% unchanged.
    *   Add `@workflow.signal(name="approve_and_execute")` accepting a serializable `ApprovedExecutionSignalInput` dataclass containing the `ApprovedPlanContext` and action parameters.
    *   At Step 4 (`AWAITING_APPROVAL`), `workflow.wait_condition` wakes when `self._cancel_requested or self._resolution_status is not None or self._approved_execution is not None`.
2.  **Replay-Safe Execution Branch (`workflow.patched("stage3-b2-b3-execution-v1")`)**:
    *   When `self._approved_execution is not None` (and not cancelled):
        1.  Execute `b1_project_incident_status(target_status=IncidentStatus.EXECUTING.value)`.
        2.  Execute activity `stage3_execute_approved_action(ApprovedActionActivityInput)` which runs:
            *   P2 `verify_approved_plan_context`
            *   E8 `MutationCapabilityGate.verify_mutation_allowed`
            *   B2 `ResourceReservationManager.acquire_reservation` (`FREE -> RESERVED(fence=n)`)
            *   B3 `ActionGateway.dispatch_and_reconcile` (`PREPARED -> SENT -> CONFIRMED | UNKNOWN`, and if `UNKNOWN`, invokes `EffectReconciler` up to `max_reconcile_attempts` until resolved to `CONFIRMED | FAILED | NEEDS_REVIEW`).
        3.  If the activity output reports receipt state `CONFIRMED`:
            *   Execute `b1_project_incident_status(target_status=IncidentStatus.VERIFYING.value)`.
            *   Execute activity `stage3_verify_and_finalize_effect(VerifyEffectActivityInput)` which runs `IndependentPostconditionVerifier` (`read_resource`), transitions receipt `CONFIRMED -> VERIFIED | NEEDS_REVIEW`, and releases the B2 reservation (`DISPATCHED -> RELEASED`).
            *   Execute `b1_project_incident_status` with the resulting terminal status (`VERIFIED` or `NEEDS_REVIEW`).
        4.  If the dispatch/reconciliation activity output reports `FAILED` or `NEEDS_REVIEW` before reaching `CONFIRMED`:
            *   Execute `b1_project_incident_status` directly from `EXECUTING` to `FAILED` or `NEEDS_REVIEW` (both are legal transitions from `EXECUTING` in `ALLOWED_TRANSITIONS`).

## Exact Schema Specification (`0002_stage3_b2_b3_e8.py`)

In `backend/persistence/alembic/versions/0002_stage3_b2_b3_e8.py` (`down_revision = "b3_005_claim_payload"`):
1.  **`adapter_conformance` table**: Created idempotently if not exists with `chk_adapter_conformance_status_valid` and `chk_adapter_retention_nonnegative`.
2.  **`resource_reservation` refinements**:
    *   Replace `ux_active_reservation_per_target` and `ix_reservation_active_expiry` so their `WHERE` clause covers `state IN ('RESERVED', 'DISPATCHED')`.
    *   Add `chk_reservation_state_valid CHECK (state IN ('RESERVED', 'DISPATCHED', 'RELEASED', 'EXPIRED', 'CANCELLED'))`.
3.  **`effect_receipt` additive columns & constraint**:
    *   Add `adapter_digest VARCHAR NOT NULL DEFAULT ''`
    *   Add `last_error VARCHAR NULL`
    *   Add `chk_effect_receipt_state_valid CHECK (state IN ('PREPARED', 'SENT', 'UNKNOWN', 'CONFIRMED', 'VERIFIED', 'FAILED', 'NEEDS_REVIEW'))` (all 29 existing rows in `opsblaze_test` have states within this exact set: `VERIFIED`, `FAILED`, `NEEDS_REVIEW`, `PREPARED`, `SENT`).
4.  **`downgrade()`**: Drops `chk_effect_receipt_state_valid`, drops `last_error` and `adapter_digest` from `effect_receipt`, drops `chk_reservation_state_valid`, restores `ux_active_reservation_per_target` and `ix_reservation_active_expiry` to `state = 'RESERVED'`, and drops `adapter_conformance`.

## EGM Review Resolution Summary
*   **Resolved `[CRITICAL]` from `egm-implementation-readiness` (Temporal Workflow Signal & Activity Boundaries)**: Specified the exact `@workflow.signal(name="approve_and_execute")`, `workflow.patched("stage3-b2-b3-execution-v1")`, and two-stage activity progression (`EXECUTING` -> `stage3_execute_approved_action` -> `VERIFYING` -> `stage3_verify_and_finalize_effect` -> `VERIFIED | FAILED | NEEDS_REVIEW`).
*   **Resolved `[CRITICAL-2]` from `egm-design` (Expired `SENT` sweep and `claim_token` CAS)**: Specified that `EffectReconciler` claims expired `SENT` rows and transitions them `SENT -> UNKNOWN` with a new `claim_token`, and that all post-I/O updates enforce `claim_token` CAS.
*   **Resolved `[IMPORTANT-4]` from `egm-design` & `egm-implementation-readiness` (Deterministic `UNKNOWN` decision table & dual-layer retention horizon check)**: Specified the 6-branch decision table in CUJ 4.
*   **Resolved `[IMPORTANT]` from `egm-zoom-out` (Single atomic pre-dispatch transaction & minimal P2 boundary)**: Unified `RESERVED -> DISPATCHED` and `PREPARED -> SENT` into one short transaction and kept the P2 boundary to `ApprovedPlanContext` + `verify_approved_plan_context()`.
