# ADR-0003: Feature B2 — Version-Fenced Resource Reservation Architecture & Design Specification

*   **Author:** OpsBlaze Architecture Team
*   **Status:** Approved (Post-EGM Goldfish Review)
*   **Date:** 2026-11-07

--------------------------------------------------------------------------------

## Overview

Feature B2 implements Version-Fenced Resource Reservation for OpsBlaze (TITAN-20 Stage 3). It serializes supported external mutations per `(tenant_id, target_id)`, allocates strictly monotonic fencing tokens from PostgreSQL (`resource_epoch`), enforces lease expiration and reclamation while preventing split-brain reservation during unresolved B3 effects, validates target resource versions and E8 adapter conformance before granting a `MutationPermit`, and guarantees that delayed or zombie workers holding stale fences are deterministically rejected at both the database layer and the target simulator/adapter boundary.

## The Problem

Distributed workflow workers can pause due to GC stalls, network delays, Temporal activity retries, or process restarts. If Worker 1 acquires a reservation with a lease that expires while Worker 1 is stalled, Worker 2 may legitimately acquire a new reservation for the same `(tenant_id, target_id)` and mutate the target resource. If Worker 1 then wakes up and sends its delayed mutation request, a naive lock-release system without monotonic fencing tokens and target-version compare-and-swap (`CAS`) will corrupt the target resource or overwrite Worker 2's changes. Furthermore, if Worker 1 has already dispatched an external mutation (`DISPATCHED` with a B3 receipt in `SENT` or `UNKNOWN`), allowing Worker 2 to expire the reservation and start a second mutation *before* the first mutation's outcome is reconciled would violate per-target mutation serializability.

## Goals

*   **G1 — Strictly Monotonic Per-Target Fencing (`resource_epoch`)**: Allocate strictly increasing `BigInteger` fencing tokens (`fence = latest_fence + 1`, `fence >= 1`) per `(tenant_id, target_id)` inside a short PostgreSQL transaction using `INSERT ... ON CONFLICT (tenant_id, target_id) DO NOTHING` followed by `SELECT ... FOR UPDATE` on `resource_epoch`. Never reuse, decrement, or reset fences after release, cancellation, or expiration.
*   **G2 — At-Most-One Active Reservation per `(tenant_id, target_id)`**: Enforce that at most one reservation in an active state (`RESERVED` or `DISPATCHED`) exists per `(tenant_id, target_id)` via both row-level locking on `resource_epoch` and a partial unique index `ux_active_reservation_per_target` covering `state IN ('RESERVED', 'DISPATCHED')`.
*   **G3 — Strict Reservation Lifecycle & Safe Lease Expiration**: Implement the state machine:
    *   `FREE -> RESERVED(fence=n) -> DISPATCHED -> RELEASED`
    *   `RESERVED -> EXPIRED -> RESERVED(fence=n+1)`
    *   `RESERVED -> CANCELLED`
    *   `DISPATCHED` reservations with an unresolved B3 `effect_receipt` (`SENT`, `UNKNOWN`, `CONFIRMED`) are protected against premature takeover until B3 reconciles the receipt to a terminal state (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`).
    *   Any stale fence or expired lease attempting to transition or dispatch -> `REJECTED` (`StaleFenceError` / `ReservationExpiredError`).
*   **G4 — Precondition & Capability Gate Enforcement**: Before granting a `MutationPermit`, verify in order:
    1.  Authenticated `tenant_id` matches the incident's `tenant_id` and the incident is in an eligible state (`AWAITING_APPROVAL` or `EXECUTING`).
    2.  A valid, non-expired `ApprovedPlanContext` (the fail-closed P2 approval boundary) matches the exact `(tenant_id, incident_id, target_id, plan_digest, action_digest, expected_version)` and has an authorized human/simulator role (never `"agent"`, `"model"`, or `"investigator"`).
    3.  The target E8 adapter is `CONFORMANT` with matching `version` and `code_digest`, non-expired attestation, and supports fencing, idempotency, status lookup, and version CAS.
    4.  The expected target resource version matches both the live target resource version from `read_resource(tenant_id, target_id)` and `resource_epoch.version_seen` (when previously observed).
*   **G5 — Dual-Layer Stale Worker Rejection**: Reject stale fences (`fence < resource_epoch.latest_fence` or reservation state not active or `expires_at <= now()`) both in PostgreSQL and at the E8 target simulator/adapter (`fence < target.highest_fence_seen`).

## Non-Goals

*   Exposing public REST endpoints for arbitrary callers or LLM agents to mint fences or reserve resources directly.
*   Holding PostgreSQL transactions or `FOR UPDATE` row locks open across external adapter network calls.

## Critical User Journeys (CUJs)

### CUJ 1: Barrier-Synchronized Contention on the Same `(tenant_id, target_id)`
*   **Given**: Two concurrent incidents `I1` and `I2` in Tenant A targeting `sim-db-primary` at version `v1`.
*   **When**: Both workers hit a synchronization barrier and simultaneously call `acquire_reservation(...)`.
*   **Then**: Both transactions execute `INSERT INTO resource_epoch ... ON CONFLICT DO NOTHING` and serialize cleanly on `SELECT ... FROM resource_epoch WHERE tenant_id = :t AND target_id = :target FOR UPDATE`. The first transaction increments `latest_fence` from `0` to `1`, inserts `resource_reservation(state='RESERVED', fence=1)`, and returns `MutationPermit(fence=1)`. The second transaction sees the active reservation for `sim-db-primary`, emits `reservation.conflict`, and raises `ResourceAlreadyReservedError` without advancing `latest_fence`.

### CUJ 2: Lease Expiration, Fence Advancement, and Delayed Worker Rejection
*   **Given**: Worker 1 holds `RESERVED(fence=1, expires_at=T1)` on `sim-db-primary`, then stalls past `T1` before dispatching to B3.
*   **When**: At `T2 > T1`, Worker 2 requests a reservation on `sim-db-primary`.
*   **Then**: `ResourceReservationManager` atomically marks Worker 1's expired `RESERVED` row as `EXPIRED` (emitting `lease.expired`), increments `resource_epoch.latest_fence` to `2`, creates Worker 2's reservation `RESERVED(fence=2)`, and Worker 2 dispatches and mutates the target to `v2` (`highest_fence_seen=2`). When Worker 1 wakes up at `T3 > T2` and attempts to dispatch with `fence=1`, both B3's database pre-dispatch check (`fence=1 < latest_fence=2`, reservation `EXPIRED`) and the E8 target simulator (`fence=1 < highest_fence_seen=2`) reject Worker 1's request and emit `fence.stale_reject`.

### CUJ 3: Target Version Drift Detection (ABA / Out-of-Band Mutation)
*   **Given**: An approved repair plan expecting `sim-db-primary` at `expected_version="v1"`, while an external change or prior mutation has moved the target to `"v2"`.
*   **When**: The worker attempts to acquire a reservation or dispatch using `expected_version="v1"`.
*   **Then**: The manager detects `expected_version ("v1") != live_snapshot.current_version ("v2")`, commits `resource_epoch.version_seen = "v2"` in a short transaction so the observed drift is durably recorded, emits `target.version_conflict`, and raises `TargetVersionConflictError` without allocating a reservation or advancing `latest_fence`.

## Technical Plan (Architecture)

```text
ApprovedPlanContext (Fail-Closed P2 Boundary) + E8 MutationCapabilityGate
                                   |
                                   v
         1. Read live target state via adapter.read_resource(tenant_id, target_id)
            (Outside DB transaction — zero locks held during I/O)
                                   |
                                   v
         2. Short PostgreSQL Transaction (< 10ms) in ResourceReservationManager:
            a. INSERT INTO resource_epoch (tenant_id, target_id, latest_fence, version_seen)
               VALUES (:tenant_id, :target_id, 0, NULL)
               ON CONFLICT (tenant_id, target_id) DO NOTHING
            b. SELECT * FROM resource_epoch
               WHERE tenant_id = :tenant_id AND target_id = :target_id FOR UPDATE
            c. Sync epoch watermark:
               latest_fence = max(epoch.latest_fence, live_snapshot.highest_fence_seen)
            d. Check version conflict:
               If expected_version != live_snapshot.current_version OR
                  (epoch.version_seen IS NOT NULL AND expected_version != epoch.version_seen):
                 Update epoch.version_seen = live_snapshot.current_version, commit tx,
                 emit 'target.version_conflict', raise TargetVersionConflictError.
               Else set epoch.version_seen = live_snapshot.current_version.
            e. Check existing active reservation (state IN ('RESERVED', 'DISPATCHED')):
               - If active row has state == 'RESERVED' and expires_at <= now:
                   Transition state -> 'EXPIRED', emit 'lease.expired'.
               - If active row has state == 'DISPATCHED' and expires_at <= now:
                   Check if an unresolved effect_receipt (state IN ('SENT', 'UNKNOWN', 'CONFIRMED'))
                   exists for (tenant_id, target_id, fence=active.fence):
                   * If unresolved receipt exists: DO NOT expire; emit 'reservation.conflict'
                     and raise ResourceUncertainEffectPendingError (subclass of ResourceAlreadyReservedError).
                   * If no unresolved receipt exists: transition state -> 'EXPIRED', emit 'lease.expired'.
               - If an active non-expired row still exists:
                   * If held by same (holder_incident_id, action_digest) and state == 'RESERVED':
                     return idempotent MutationPermit for existing fence.
                   * Otherwise emit 'reservation.conflict' and raise ResourceAlreadyReservedError.
            f. Allocate new fence:
               new_fence = epoch.latest_fence + 1
               epoch.latest_fence = new_fence
            g. Insert resource_reservation(state='RESERVED', fence=new_fence, expires_at=now+ttl)
            h. Commit transaction & return immutable server-minted MutationPermit
```

### Schema Refinements in `0002_stage3_b2_b3_e8.py`
In the baseline `b3_005_claim_payload` schema, `ux_active_reservation_per_target` and `ix_reservation_active_expiry` had `WHERE (state = 'RESERVED')`. Because a reservation transitions `RESERVED -> DISPATCHED -> RELEASED` while an external mutation is in flight, a reservation in `DISPATCHED` state is ALSO active and must block competing reservations until it is `RELEASED` or `EXPIRED`.
*   In `0002_stage3_b2_b3_e8.py`:
    1.  Drop index `ux_active_reservation_per_target` and recreate it with `postgresql_where=sa.text("(state)::text = ANY ((ARRAY['RESERVED'::character varying, 'DISPATCHED'::character varying])::text[])")`.
    2.  Drop index `ix_reservation_active_expiry` and recreate it with the same `RESERVED` / `DISPATCHED` predicate.
    3.  Add `CheckConstraint("state IN ('RESERVED', 'DISPATCHED', 'RELEASED', 'EXPIRED', 'CANCELLED')", name="chk_reservation_state_valid")` on `resource_reservation`.
*   Because `resource_reservation` has `0` rows in both `opsblaze` and `opsblaze_test`, updating these two partial indexes and adding the check constraint is instantaneous and 100% non-destructive.

### Exact Columns Verified During B3 Pre-Dispatch Claim
When B3's `ActionGateway` transitions a reservation from `RESERVED -> DISPATCHED` (atomically alongside `effect_receipt` `PREPARED -> SENT` in a single short transaction), it locks `resource_epoch` and `resource_reservation` with `FOR UPDATE` and verifies:
1.  `reservation.id == permit.reservation_id`
2.  `reservation.tenant_id == permit.tenant_id`
3.  `reservation.target_id == permit.target_id`
4.  `reservation.holder_incident_id == permit.incident_id`
5.  `reservation.action_digest == permit.action_digest`
6.  `reservation.fence == permit.fence` and `permit.fence == resource_epoch.latest_fence`
7.  `reservation.state == 'RESERVED'` (or `'DISPATCHED'` on a controlled retry of the same receipt)
8.  `reservation.expires_at > now`
9.  `resource_epoch.version_seen == permit.expected_version`

If `reservation.expires_at <= now`, `ActionGateway` marks the reservation `EXPIRED`, commits that state update before raising, emits `lease.expired` and `fence.stale_reject`, and raises `StaleFenceError`.

### EGM Review Resolution Summary
*   **Resolved `[CRITICAL-1]` (Unresolved receipt protection on `DISPATCHED` leases)**: `DISPATCHED` reservations with an unresolved B3 receipt (`SENT`, `UNKNOWN`, `CONFIRMED`) are never expired to let a competing incident start a concurrent mutation until B3 reconciles the receipt to a terminal state.
*   **Resolved `[IMPORTANT-1]` (Concurrent first-time `resource_epoch` creation)**: Added `INSERT INTO resource_epoch ... ON CONFLICT (tenant_id, target_id) DO NOTHING` before `SELECT ... FOR UPDATE`.
*   **Resolved `[IMPORTANT-3]` (Durable `version_seen` update on conflict)**: When `TargetVersionConflictError` is detected, `resource_epoch.version_seen` is updated and committed in a short transaction before raising the exception.
*   **Resolved `[IMPORTANT]` from `egm-zoom-out` (Single cohesive B2 module)**: Consolidated B2 logic into `backend/effects/fencing.py` (`ResourceReservationManager`, `MutationPermit`, and reservation state machine) without redundant pass-through service wrappers.
