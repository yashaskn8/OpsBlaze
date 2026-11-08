# ADR-0005: Stage 4 — Durable Integrity, Cross-Store Recovery & Historical Effect Safety

* **Status:** Accepted (Reviewed & Hardened via EGM Goldfish Swarm: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`)
* **Date:** 2026-11-07
* **Authors:** OpsBlaze Architecture & Reliability Engineering
* **Capabilities Covered:** Stage 4 Durable Integrity across `B1` (Durable Incident Workflow), `B2` (Version-Fenced Resource Reservation), `B3` (Effect Receipt and Reconciliation), and `E8` (Adapter Conformance and Chaos Lab)
* **Depends On:** [0001-b1-durable-incident-workflow.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md), [0002-e8-adapter-conformance-and-chaos-lab.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-e8-adapter-conformance-and-chaos-lab.md), [0003-b2-version-fenced-resource-reservation.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0003-b2-version-fenced-resource-reservation.md), [0004-b3-effect-receipt-and-reconciliation.md](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0004-b3-effect-receipt-and-reconciliation.md)

---

## 1. Context and Problem Statement

OpsBlaze maintains two independent durable persistence authorities plus external/simulated target resources:
1. **PostgreSQL (`opsblaze` / `opsblaze_test`)** — Authoritative domain store for `tenant`, `incident`, `outbox`, `resource_epoch`, `resource_reservation`, `effect_receipt`, and `adapter_conformance`.
2. **Temporal Persistence (`.local/temporal.db` or Temporal service history)** — Authoritative workflow execution history for `IncidentWorkflow` (`incident:{tenant_id}:{incident_id}`).
3. **Target Resource / Adapter State (`TargetAdapterProtocol`)** — External or simulated resource state holding `current_version`, `highest_fence_seen`, `last_effect_id`, and bounded effect retention history.

There is no distributed two-phase commit across PostgreSQL, Temporal, and external target adapters. During disaster recovery, process crashes, migration rollbacks, or point-in-time snapshot restores:
- **Scenario A (PostgreSQL Behind Temporal):** PostgreSQL is restored to an earlier snapshot while Temporal retains newer workflow execution histories (or target resources retain higher `highest_fence_seen` watermarks).
- **Scenario B (Temporal Behind PostgreSQL):** Temporal persistence is restored to an earlier snapshot (or started fresh after loss) while PostgreSQL contains newer `incident`, `outbox`, `resource_reservation`, and `effect_receipt` rows.
- **Scenario C (Divergent Independent Restores):** Both PostgreSQL and Temporal are restored independently to divergent points in time while live target adapters reflect mutations from after one or both snapshots.
- **Historical / Legacy `effect_receipt` Rows:** Pre-Stage-3 `effect_receipt` rows (such as the 29 historical rows in `opsblaze_test` from revision `b3_005_claim_payload`) may be in non-terminal states (`PREPARED`, `SENT`) with `claim_token IS NULL`, `lease_expires_at IS NULL`, `action_payload IS NULL`, `adapter_digest = ''`, or uncertified/unregistered `adapter_id` values, and without corresponding `resource_reservation` rows.
- **Workflow History Compatibility:** Workers upgraded from B1-only to Stage 3/4 must deterministically replay archived B1 workflow histories, Stage 3 workflow histories, workflows waiting at `AWAITING_APPROVAL`, cancelled workflows, and activity retry histories without `NonDeterminismError`.

---

## 2. Non-Negotiable Recovery & Safety Invariants

| ID | Invariant | Enforcement Mechanism |
|---|---|---|
| **R1** | **Degraded Recovery Mode Blocks New Mutations & Background Dispatch** | While `DegradedRecoveryGate.is_degraded()` is `True`, `ResourceReservationManager.acquire_reservation()`, `ActionGateway.execute_with_permit()`, and `OutboxDispatcher.dispatch_once()` fail closed with `DegradedRecoveryModeError` (or pause dispatch), while `EffectReconciler.reconcile_pending()` and `ActionGateway.verify_and_finalize_confirmed_receipt()` remain operational so recovery Step 9 can finalize in-flight receipts. If any recovery step fails, the gate remains fail-closed (`is_degraded() == True`). |
| **R2** | **No Blind Re-Apply of Historical or Unresolved `SENT` / `PREPARED` Receipts** | Historical `PREPARED` receipts (where `state = 'PREPARED'` during recovery or `claim_token IS NULL` or `adapter_digest == ''`) and legacy `SENT` receipts (where `claim_token IS NULL` or `action_payload IS NULL` or `action_digest == ''` or `adapter_digest == ''`) are **never** blindly re-dispatched via `adapter.apply()`. Legacy `PREPARED` receipts are quarantined directly to `NEEDS_REVIEW` (`last_error='legacy_prepared_quarantined_on_recovery'`), and legacy `SENT` receipts are reconciled strictly via read-only `adapter.query_effect()`. For legacy receipts (`adapter_digest == ''` or `action_payload IS NULL`), even if `query_effect()` returns `NOT_FOUND` within the retention window (e.g., after an in-memory simulator restart), the receipt transitions to **`NEEDS_REVIEW`** (`last_error='legacy_sent_unverifiable_not_found'`), never `FAILED`, because `NOT_FOUND` on a legacy/unverified or restarted in-memory target does not prove non-execution. |
| **R3** | **Fence Watermark Monotonicity Across PG Restore & Target Restart** | Both during recovery (Step 7) and at runtime inside `ResourceReservationManager.acquire_reservation()`, `resource_epoch.latest_fence` for `(tenant_id, target_id)` is advanced to `max(epoch.latest_fence, target.highest_fence_seen, COALESCE(max(resource_reservation.fence), 0), COALESCE(max(effect_receipt.fence), 0))`. Neither a restored PostgreSQL snapshot nor a restarted simulator (`highest_fence_seen == 0`) can ever cause a duplicate or lower fence token to be minted than one already recorded in `resource_reservation`, `effect_receipt`, or the target. |
| **R4** | **Target State Ahead of Restored PostgreSQL Fails Closed** | In Step 7, if `live_snapshot.highest_fence_seen > db_max_fence` or (`live_snapshot.last_effect_id` is non-empty and no matching `EffectReceipt` with `idempotency_key == live_snapshot.last_effect_id` exists in PostgreSQL), `resource_epoch.latest_fence` and `version_seen` are synchronized to the live target, any active `RESERVED` or `DISPATCHED` reservation on that target with `fence <= live_snapshot.highest_fence_seen` is released/expired, and its holder incident (if non-terminal) is transitioned to `NEEDS_REVIEW`. |
| **R5** | **Cross-Store Workflow & Outbox Convergence After Receipt Resolution** | Incident status projection against Temporal and `effect_receipt` runs in Step 6 and is finalized after Step 9 receipt reconciliation: every incident with a terminal `effect_receipt` (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`) projects monotonically forward via `IncidentRepository.reconcile_status_projection_forward()` to the corresponding terminal status (and marks `outbox.delivered_at = now`); incidents in `EXECUTING` or `VERIFYING` with no workflow and no receipt are quarantined to `NEEDS_REVIEW`; pre-execution incidents (`DISPATCH_PENDING`, `INVESTIGATING`, `AWAITING_APPROVAL`) whose Temporal workflow was lost reset `incident.status_projection = DISPATCH_PENDING` and `outbox.delivered_at = NULL` so `OutboxDispatcher` cleanly re-runs read-only B1 investigation to `AWAITING_APPROVAL`. |
| **R6** | **Deterministic Replay Across B1 and Stage 3 Histories** | `IncidentWorkflow` preserves `workflow.patched("b1-workflow-v1")` and `workflow.patched("stage3-b2-b3-execution-v1")` in exact chronological order so Temporal `Replayer` passes with zero `NonDeterminismError` across B1-only and Stage 3 histories. |

---

## 3. Deterministic 10-Step Cross-Store Recovery Sequence

Implemented in [`backend/workflows/recovery.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workflows/recovery.py) via `CrossStoreRecoveryCoordinator` and `DegradedRecoveryGate`:

1. **Step 1 — Enter Degraded Recovery Mode:**
   - Activate `DegradedRecoveryGate.enter_degraded_mode(reason=...)` and emit `recovery.degraded_mode_entered`.
2. **Step 2 — Disable New Consequential Actions:**
   - Verify that `ResourceReservationManager.acquire_reservation()`, `ActionGateway.execute_with_permit()`, and `OutboxDispatcher.dispatch_once()` reject new mutations/dispatches with `DegradedRecoveryModeError` while allowing read-only inspection and recovery reconciliation (`EffectReconciler.reconcile_pending()` and `ActionGateway.verify_and_finalize_confirmed_receipt()`).
3. **Step 3 — Verify PostgreSQL Schema & Revision Integrity:**
   - Query `alembic_version` and verify it matches the expected head (`stage3_002_b2_b3_e8`) and that all 8 authoritative tables (`tenant`, `incident`, `outbox`, `resource_epoch`, `resource_reservation`, `effect_receipt`, `adapter_conformance`, `alembic_version`) are accessible.
4. **Step 4 — Validate Temporal Connectivity & Snapshot State:**
   - Inspect the provided Temporal workflow state map or query active/closed Temporal workflow executions for the recovery scope.
5. **Step 5 — Reconcile Durable Admission Intents (`outbox` vs `incident`):**
   - For any `incident` in `ACCEPTED` that is missing an `outbox` row, insert a recovery `INCIDENT_START` outbox row.
   - For any undelivered `outbox` row (`delivered_at IS NULL`) whose `incident` is already in a terminal status (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`, `CANCELLED`), mark `delivered_at = now` so the dispatcher never starts a redundant workflow.
6. **Step 6 — Initial Workflow Identity (`incident:{tenant_id}:{incident_id}`) Alignment:**
   - Compare PostgreSQL `incident` rows against known Temporal workflow executions (both running and closed).
   - **Scenario A (Temporal ahead of PostgreSQL):** If Temporal holds a running or completed workflow execution whose status is further ahead in the B1 lifecycle than `incident.status_projection`, advance `incident.status_projection` monotonically via `IncidentRepository.reconcile_status_projection_forward()` and mark `outbox.delivered_at = now`.
7. **Step 7 — Reconcile B2 Resource Epochs & Reservations (`resource_epoch` vs `resource_reservation` vs `effect_receipt` vs Live Target):**
   - Using bulk grouped queries (`MAX(fence) GROUP BY tenant_id, target_id` on `resource_reservation` and `effect_receipt`), compute `db_max_fence = max(epoch.latest_fence, max_reservation_fence, max_receipt_fence)` for every `(tenant_id, target_id)`.
   - If a registered adapter exists for the target, call read-only `adapter.read_resource(tenant_id, target_id)` and compute `effective_fence = max(db_max_fence, live_snapshot.highest_fence_seen)`.
   - Upsert/update `resource_epoch.latest_fence = effective_fence` and `resource_epoch.version_seen = live_snapshot.current_version`.
   - Enforce **Invariant R4**: if `live_snapshot.highest_fence_seen > db_max_fence` or (`live_snapshot.last_effect_id` is non-empty and no `EffectReceipt` with `idempotency_key == live_snapshot.last_effect_id` exists in PG), release/expire any active reservation on `(tenant_id, target_id)` with `fence <= live_snapshot.highest_fence_seen` and quarantine its holder incident to `NEEDS_REVIEW`.
   - Release any `RESERVED` or `DISPATCHED` reservation whose associated `effect_receipt` is already in `TERMINAL_EFFECT_STATES` (`RELEASED`), and expire any `RESERVED` or `DISPATCHED` reservation whose `expires_at <= now` when no unresolved `effect_receipt` (`SENT`, `UNKNOWN`, `CONFIRMED`) exists (`EXPIRED`).
8. **Step 8 — Quarantine Orphaned / Legacy `PREPARED` Receipts:**
   - Identify all `PREPARED` receipts (`state = 'PREPARED'`) where `claim_token IS NULL`, `adapter_digest == ''`, or `lease_expires_at IS NULL` or `lease_expires_at <= now`.
   - Quarantine each directly to `NEEDS_REVIEW` using `validate_effect_transition(EffectState.PREPARED, EffectState.NEEDS_REVIEW)` and release any associated B2 reservation via `ActionGateway._release_reservation_for_receipt()`.
9. **Step 9 — Reconcile `SENT`, `UNKNOWN`, and `CONFIRMED` Receipts & Converge Incident Projections:**
   - Invoke `EffectReconciler.reconcile_pending(allow_controlled_retry=False)` to claim all `UNKNOWN` and expired/null-lease `SENT` receipts via `FOR UPDATE SKIP LOCKED`, transition `SENT -> UNKNOWN`, and reconcile each via read-only `adapter.query_effect()` and `IndependentPostconditionVerifier`.
   - For any `CONFIRMED` receipt interrupted before postcondition verification: if the adapter is registered, invoke `ActionGateway.verify_and_finalize_confirmed_receipt()`; if the adapter is unregistered, transition `CONFIRMED -> NEEDS_REVIEW` and release its B2 reservation.
   - **Final Cross-Store Incident Convergence (Scenario B & C):**
     - For any non-terminal `incident` that now has a terminal `effect_receipt` (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`), project `incident.status_projection` monotonically forward to that terminal status (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`) and set `outbox.delivered_at = now`.
     - For any `incident` stuck in `EXECUTING` or `VERIFYING` with no Temporal workflow and no `effect_receipt`, transition `incident.status_projection` to `NEEDS_REVIEW` and release any active reservation.
     - For any pre-execution `incident` (`DISPATCH_PENDING`, `INVESTIGATING`, `AWAITING_APPROVAL`) whose Temporal workflow is missing (or terminated/failed in Temporal), reset `incident.status_projection = DISPATCH_PENDING` (bumping `status_version`) and `outbox.delivered_at = NULL, available_at = now` so `OutboxDispatcher` cleanly re-runs the deterministic B1 read-only investigation workflow to `AWAITING_APPROVAL`.
10. **Step 10 — Verify Post-Recovery Invariants and Exit Degraded Mode:**
    - Verify in PostgreSQL:
      1. Zero orphaned `PREPARED` receipts remain in the recovery scope.
      2. Zero `resource_epoch.latest_fence` values lag behind `resource_reservation.fence` or `effect_receipt.fence`.
      3. Zero active (`RESERVED` / `DISPATCHED`) reservations remain for terminal receipts.
    - Call `DegradedRecoveryGate.exit_degraded_mode()` and emit `recovery.completed`.

---

## 4. Historical Effect Receipt Safety Contract (Phase 5)

To protect the 29 historical `effect_receipt` rows in `opsblaze_test` (and any pre-Stage-3 production database upgraded from `b3_005_claim_payload`):
1. `ALLOWED_EFFECT_TRANSITIONS[EffectState.PREPARED]` includes `EffectState.NEEDS_REVIEW` so that interrupted or legacy `PREPARED` receipts are quarantined to `NEEDS_REVIEW` during recovery without fabricating a `FAILED` external execution result or dispatching `SENT`.
2. In `EffectReconciler._reconcile_single()`, a controlled retry (`UNKNOWN -> SENT`) is **strictly forbidden** if:
   - `not allow_controlled_retry`, OR
   - `snap.action_payload is None` or `not snap.action_digest`, OR
   - `not snap.adapter_digest` (legacy pre-Stage-3 receipt without E8 conformance digest), OR
   - No active `DISPATCHED` `ResourceReservation` row exists in PostgreSQL with `fence == snap.fence` and `expires_at > now`.
3. If `adapter.query_effect()` returns `AdapterLookupStatus.NOT_FOUND` for a **legacy receipt** (`not snap.adapter_digest` or `snap.action_payload is None`), `EffectReconciler._reconcile_single()` transitions the receipt to **`NEEDS_REVIEW`** (`last_error="Effect not found at target for legacy unverified receipt; human review required"`), **never** `FAILED`, preventing false-negative assumptions when an in-memory simulator or uncertified legacy adapter loses state.
4. Terminal rows (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`) are excluded from all claim queries and protected by `validate_effect_transition()`, guaranteeing strict immutability.

---

## 5. Stage 3 Concurrency-20 `get_receipt` Latency Investigation (Phase 9)

During Stage 3 benchmarking (`scripts/stage3_perf_stress.py`), `effect_receipt_lookup` at `concurrency=20` exhibited `p50=94.55ms` (`p99=234.67ms`) compared to `p50=10.15ms` at `concurrency=10`.
Root-cause analysis:
1. Primary-key lookup `SELECT ... FROM effect_receipt WHERE tenant_id = :t AND id = :id` uses `effect_receipt_pkey (id)` in PostgreSQL (`< 0.1ms` execution time in `EXPLAIN (ANALYZE, BUFFERS)`).
2. In `scripts/stage3_perf_stress.py`, each concurrency iteration ran immediately after 20 concurrent multi-transaction `different_target_b2_b3_execution` workflows on a single Windows ProactorEventLoop and asyncpg connection pool (`pool_size=10, max_overflow=10`), where connection checkout/reset and cold overflow TCP/SSL connection establishment on `localhost:5432` dominated wall-clock latency when all 20 coroutines hit the barrier simultaneously while `pool_size=10` required 10 overflow connections.
3. Stage 4 verifies the PostgreSQL query plan (`Index Scan using effect_receipt_pkey`) and benchmarks both `pool_size=10, max_overflow=10` (overflow connection creation under burst) and pre-warmed `pool_size=20, max_overflow=10` alongside full Stage 4 recovery benchmarks in `scripts/stage4_recovery_benchmark.py`.
