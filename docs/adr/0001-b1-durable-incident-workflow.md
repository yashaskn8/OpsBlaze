# ADR-0001: Feature B1 — Durable Incident Workflow Architecture & Design Specification

*   **Author:** OpsBlaze Architecture Team
*   **Status:** Approved (Post-EGM Goldfish Review)
*   **Date:** 2026-11-07

--------------------------------------------------------------------------------

## Overview

Feature B1 establishes the Durable Incident Workflow foundation for OpsBlaze (TITAN-20). It provides tenant-scoped, idempotent incident admission backed by PostgreSQL, an atomic transactional outbox, an asynchronous outbox dispatcher process, a deterministic Temporal workflow (`IncidentWorkflow`) with typed read-only fake investigator activities, versioned status projections, and a minimal React and TypeScript status interface.

## The Problem

Incident investigation and recovery workflows span multiple failure domains: HTTP API processes, relational database persistence, workflow scheduling, worker restarts, and human review waits. Relying on in-process background tasks loses work when an API server crashes, while calling an external workflow engine inside a database transaction holds database locks across network I/O and cannot atomically commit across two independent data stores. OpsBlaze requires a durable incident lifecycle where every admitted incident is persisted once per `(tenant_id, client_key)`, guaranteed to start at most one logical workflow via a transactional outbox and deterministic workflow ID, and projected safely to operators without exposing cross-tenant data or executing unapproved external mutations.

## Goals

*   **G1 — Tenant-Scoped Idempotent Admission**: Enforce one logical incident per `(tenant_id, client_key)` with canonical SHA-256 request payload hashing (`HTTP 202` for new or identical-payload replay, `HTTP 409` for changed-payload conflict).
*   **G2 — Atomic Admission and Outbox Intent**: Persist the `incident` row and `outbox` start intent inside a single short PostgreSQL transaction without performing network calls inside the transaction.
*   **G3 — Recoverable Outbox Dispatch**: Claim eligible outbox entries using `FOR UPDATE SKIP LOCKED` in short transactions, start Temporal workflows using a deterministic workflow ID `incident:{tenant_id}:{incident_id}`, handle duplicate starts idempotently, and apply bounded retry backoff on failure.
*   **G4 — Deterministic Temporal Workflow & Fake Investigator**: Execute `IncidentWorkflow` deterministically with typed input/output contracts, a deterministic fixture-based read-only fake investigator activity, explicit human-wait (`AWAITING_APPROVAL`), versioned status projection updates, and race-safe cancellation.
*   **G5 — Strict B1 State Machine**: Enforce legal transitions (`ACCEPTED -> DISPATCH_PENDING -> INVESTIGATING -> AWAITING_APPROVAL -> EXECUTING -> VERIFYING -> VERIFIED | FAILED | NEEDS_REVIEW | CANCELLED`), version monotonicity (`status_version`), idempotent retry safety, and terminal-state immutability.
*   **G6 — Observability, Operations, and Minimal React UI**: Emit structured events and metrics with correlation IDs, separate `/healthz` and `/readyz` semantics, recover historical Alembic migration lineage (`b3_005_claim_payload`) non-destructively, and provide a minimal React interface where backend snapshots remain authoritative.

## Non-Goals

*   Implementing B2 (Resource Reservation & Fencing), B3 (Effect Receipt & Reconciliation), P2 (Approval Cockpit), or any external mutation adapter execution.
*   Calling real LLMs or external network tools during investigation (A1/A2 are out of scope; B1 uses a deterministic fake read-only investigator).
*   Adding Redis, Kafka, Kubernetes, LangGraph, or additional microservices.
*   Building the full P1 live SSE investigation workbench.

## Open Questions

*   None. All architectural boundaries and database preservation rules are locked by `AGENTS.md` and verified against the existing PostgreSQL schema.

## Critical User Journeys (CUJs)

### CUJ 1: Idempotent Incident Admission and Investigation Progression

*   **Given**: An authenticated tenant context (`X-Tenant-ID`) and a healthy PostgreSQL database.
*   **When**: The client submits `POST /v1/incidents` with `Idempotency-Key` and `{"target_id": "sim-db-primary", "title": "High replication lag", "severity": "SEV2"}`.
*   **Then**: The API atomically inserts the `incident` and `outbox` rows and returns `HTTP 202` with the incident snapshot. Repeating the exact request returns `HTTP 202` with the identical incident ID; repeating with the same `Idempotency-Key` and a different `target_id` or payload returns `HTTP 409`. Once the dispatcher delivers the start intent to Temporal, the workflow transitions the incident through `DISPATCH_PENDING` and `INVESTIGATING` to `AWAITING_APPROVAL` with deterministic read-only observations.

### CUJ 2: Outbox Recovery Across Dispatcher Crash and Temporal Outage

*   **Given**: An admitted incident whose `outbox` row has `delivered_at IS NULL`, and a dispatcher that crashes after Temporal accepts `start_workflow` but before `delivered_at` is committed in PostgreSQL.
*   **When**: A new or restarted dispatcher claims the reclaimed outbox row after its claim lease expires and invokes `start_workflow` with the same workflow ID `incident:{tenant_id}:{incident_id}`.
*   **Then**: Temporal identifies the existing workflow execution, the dispatcher treats `WorkflowAlreadyStartedError` as idempotent success, marks `delivered_at = now()`, and zero duplicate logical workflows are created.

### CUJ 3: Tenant-Scoped Inspection and Version-Safe Cancellation

*   **Given**: An incident in `ACCEPTED`, `DISPATCH_PENDING`, `INVESTIGATING`, or `AWAITING_APPROVAL` belonging to Tenant A.
*   **When**: Tenant B attempts `GET /v1/incidents/{id}` or `POST /v1/incidents/{id}/cancel`, or Tenant A submits `POST /v1/incidents/{id}/cancel`.
*   **Then**: Tenant B receives `HTTP 404` (preventing cross-tenant UUID enumeration). Tenant A's cancellation transitions the incident to `CANCELLED`, increments `status_version`, marks any undelivered `outbox` row for that incident as delivered/superseded in the same transaction, and cancels/signals the Temporal workflow if running.

## Technical Plan (Architecture)

```text
React + TypeScript (frontend/src/App.tsx)
        |
        v
FastAPI Incident API (backend/api/incidents.py, backend/api/main.py)
        |
        v
Short PostgreSQL Transaction (backend/persistence/repositories.py)
        |
        +------> incident (ux_incident_key: tenant_id, client_key)
        |
        +------> outbox (kind='INCIDENT_START', delivered_at IS NULL)
                       |
                       v
        Transactional Outbox Dispatcher (backend/workflows/dispatcher.py)
        [Short claim tx: FOR UPDATE SKIP LOCKED -> Temporal start -> Short ack tx]
                       |
                       v
        Temporal Server (scripts/temporal_dev.py / localhost:7233)
                       |
                       v
        IncidentWorkflow (backend/workflows/incident_workflow.py)
        [Workflow ID: incident:{tenant_id}:{incident_id}]
                       |
                       v
        Typed Activities (backend/workflows/activities.py)
        +------> project_incident_status_activity (CAS on status_version)
        +------> run_fake_investigation_activity (backend/investigator/fake.py)
```

### Explicit Architectural Decisions

1.  **Why Temporal over FastAPI `BackgroundTasks`**: FastAPI `BackgroundTasks` run in-memory inside the web worker process; any process crash, deployment restart, or OOM kills the in-flight investigation with no durable history, retry state, or timer recovery. Temporal persists every workflow event, activity schedule, retry attempt, and signal in its workflow history store, enabling deterministic replay and worker restart recovery.
2.  **Why a Transactional Outbox is Necessary**: PostgreSQL and Temporal are separate storage authorities without a distributed two-phase commit (`2PC`). Writing to PostgreSQL and calling Temporal inline risks either committing the DB row and losing the workflow start on process crash, or starting the workflow before the DB transaction rolls back. Committing `incident` and `outbox` in one atomic PostgreSQL transaction guarantees that every admitted incident has a durable start intent.
3.  **Why Workflow Start Delivery May Be Physically Repeated**: The dispatcher claims an outbox row in a short transaction, calls Temporal over the network, and then updates `delivered_at` in a second short transaction. If the dispatcher crashes or loses the network acknowledgment after Temporal accepts `start_workflow` but before `delivered_at` commits, the outbox claim expires and is retried.
4.  **How Stable Workflow Identity Prevents Duplicate Logical Execution**: Every incident maps deterministically to `workflow_id = f"incident:{tenant_id}:{incident_id}"`. When the dispatcher retries `start_workflow` with `id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE`, Temporal rejects duplicate active/completed starts with `WorkflowAlreadyStartedError`, which the dispatcher handles as idempotent delivery confirmation.
5.  **Why Database Transactions Must Not Remain Open During External I/O**: Holding a PostgreSQL transaction or `FOR UPDATE` row lock across a Temporal RPC ties up connection pool slots and row locks for the duration of network timeouts, causing connection-pool exhaustion and cascading tail latency under slow downstream dependencies.
6.  **How Workflow Code Remains Deterministic**: `IncidentWorkflow` uses only `temporalio.workflow` primitives (`workflow.execute_activity`, `workflow.wait_condition`, `workflow.patched`, `workflow.logger`). It performs zero SQL queries, zero direct HTTP calls, zero `random` calls, and zero `datetime.now()` calls.
7.  **How Worker Upgrades Preserve Old Workflow Histories**: Workflow behavioral changes are gated with `workflow.patched("b1-workflow-v1")` so replaying archived histories follows the original code path while new workflow executions take the updated path, verified by Temporal `Replayer` tests.
8.  **How the Application Behaves When Temporal Is Unavailable**: `POST /v1/incidents` continues to succeed (`HTTP 202`) as long as PostgreSQL is available because admission only writes to `incident` and `outbox`. Outbox rows remain pending (`delivered_at IS NULL`), the dispatcher records retry attempts and exponential backoff by advancing `available_at`, and `GET /v1/incidents/{id}` reports the authoritative DB status alongside `projection_lag_ms` and outbox delivery state.
9.  **Single Ownership of Status Transitions & Cancellation Races (Post-Review Resolution)**:
    *   **Single Ownership of `DISPATCH_PENDING`**: The `OutboxDispatcher` claims the outbox row alongside the parent `incident` (`tenant_id`, `target_id`, `status_projection`, `status_version`). In the claim/ack flow, if `incident.status_projection == 'CANCELLED'`, the dispatcher immediately marks `delivered_at = now()` without starting a workflow. Otherwise, when the dispatcher acknowledges Temporal start, it atomically marks `outbox.delivered_at = now()` and transitions `incident.status_projection` from `ACCEPTED` (version 1) to `DISPATCH_PENDING` (version 2) if still in `ACCEPTED`.
    *   **Idempotent Activity Status Projection**: `project_incident_status_activity` transitions `DISPATCH_PENDING -> INVESTIGATING` and `INVESTIGATING -> AWAITING_APPROVAL` (and accepts `ACCEPTED -> INVESTIGATING` if the worker executes the first activity before the dispatcher ack transaction commits). If `current_status == target_status` (from a prior activity attempt whose completion ack was lost), the repository treats it as an idempotent success and returns the current row. If `current_status == 'CANCELLED'`, `project_incident_status_activity` returns a typed `cancelled=True` result so `IncidentWorkflow` exits cleanly in `CANCELLED` without retrying or overwriting the terminal cancellation.
    *   **Concurrent Admission Rollback**: When concurrent `POST /v1/incidents` calls race on `(tenant_id, client_key)`, the losing transaction hits `IntegrityError` on `ux_incident_key`, explicitly calls `await session.rollback()`, and then queries the winning `(tenant_id, client_key)` row in a fresh read transaction to compare `request_sha256`.

### Risks & Mitigations

*   **Outbox Lease Expiry Window**: If a Temporal `start_workflow` RPC hangs longer than `lease_seconds` (default `15s`), another dispatcher may reclaim the row. Mitigation: Both dispatchers use the deterministic workflow ID `incident:{tenant_id}:{incident_id}` with `REJECT_DUPLICATE`, making concurrent starts safe.
*   **Poison-Pill / Stuck Outbox Items**: If an outbox item exceeds `stuck_attempt_threshold` (default `5` attempts) or age threshold (`60s`), the dispatcher emits `outbox.stuck` and increments `stuck_workflow_age_seconds` / `outbox_retries_total` with capped exponential backoff (`max 60s`).
*   **Database Connection Pool Saturation**: Under spike load, unbounded connections could exhaust PostgreSQL `max_connections`. Mitigation: Bounded pool (`pool_size=10`, `max_overflow=10`, `pool_timeout=5.0`) and short transactions (`< 10ms`) with explicit `HTTP 503` degradation if pool timeout is exceeded.

## Alternatives Considered

### Alternative A: FastAPI In-Process `BackgroundTasks` Without Outbox

*   **Pros**: Zero external workflow process; fewer moving parts for simple demos.
*   **Cons**: Process restarts lose in-flight incidents; no durable timer, signal, or activity retry history.
*   **Reason for Rejection**: Rejected by author: violates B1 durability, restart recovery, and `AGENTS.md` Section 4.1 constraints.

### Alternative B: Inline Temporal `start_workflow` Inside `POST /v1/incidents`

*   **Pros**: Avoids running a separate outbox dispatcher loop.
*   **Cons**: Either holds a database transaction open across a network call to Temporal or risks orphan incidents when Temporal is temporarily unreachable at admission time.
*   **Reason for Rejection**: Rejected by author: violates Cross-Feature Invariants #2 and #3 and couples incident admission availability to Temporal availability.

### Alternative C: Destructive Database Reset or `alembic stamp` for Missing Migration Files

*   **Pros**: Trivial one-command reset.
*   **Cons**: Destroys existing data in `opsblaze_test` (22 tenants, 22 incidents, 29 effect receipts) or conceals missing migration history.
*   **Reason for Rejection**: Rejected by author: strictly prohibited by Section 0 Non-Negotiable Rules. Reconstructing the exact idempotent migration chain to `b3_005_claim_payload` restores `alembic current`, `alembic heads`, and isolated fresh-schema upgrades while leaving `opsblaze` and `opsblaze_test` 100% untouched.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Privacy**: Tenant identity is extracted exclusively from authenticated server headers (`X-Tenant-ID` validated as UUID and resolved against `tenant`), never from the JSON request body. Cross-tenant reads and cancels return `HTTP 404` to prevent UUID enumeration. `.env` credentials are never logged or returned in error payloads.
*   **Performance & Latency**: Connection pooling uses bounded SQLAlchemy async queue pool settings (`pool_size=10`, `max_overflow=10`, `pool_timeout=5.0`, `pool_pre_ping=True`). Dispatcher claims run in `< 10ms` transactions using `FOR UPDATE SKIP LOCKED` over `ix_outbox_pending`.
*   **Rollout & Rollback**: Reconstructed migration `b3_005_claim_payload` uses idempotent DDL guards (`IF NOT EXISTS`) so running `alembic upgrade head` against existing databases is a no-op, while running against an isolated test schema creates the exact table, constraint, and index layout.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Exact PostgreSQL Schema Specification (`b3_005_claim_payload`)

1.  **`tenant`**:
    *   Columns: `id UUID PRIMARY KEY`, `name VARCHAR NOT NULL UNIQUE (tenant_name_key)`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
2.  **`incident`**:
    *   Columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT`, `client_key VARCHAR NOT NULL`, `request_sha256 VARCHAR NOT NULL`, `target_id VARCHAR NOT NULL`, `status_projection VARCHAR NOT NULL`, `status_version INTEGER NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
    *   Constraints: `ux_incident_key UNIQUE (tenant_id, client_key)`, `ux_incident_tenant_id UNIQUE (tenant_id, id)`.
    *   Indexes: `ix_incident_target_status (tenant_id, target_id, status_projection)`, `ix_incident_tenant_history (tenant_id, created_at DESC, id)`, `ix_incident_tenant_id (tenant_id)`.
3.  **`outbox`**:
    *   Columns: `id BIGINT PRIMARY KEY DEFAULT nextval('outbox_id_seq')`, `incident_id UUID NOT NULL REFERENCES incident(id) ON DELETE CASCADE`, `kind VARCHAR NOT NULL`, `attempt INTEGER NOT NULL`, `available_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `delivered_at TIMESTAMPTZ NULL`.
    *   Constraints: `chk_outbox_attempt_nonnegative CHECK (attempt >= 0)`.
    *   Indexes: `ix_outbox_incident_id (incident_id)`, `ix_outbox_pending (available_at, id) WHERE (delivered_at IS NULL)`.
4.  **`resource_epoch`** (Foundation table preserved for `b3_005_claim_payload` schema parity; not used by B1):
    *   Columns: `tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT`, `target_id VARCHAR NOT NULL`, `latest_fence BIGINT NOT NULL DEFAULT 0`, `version_seen VARCHAR NULL`.
    *   Constraints: `PRIMARY KEY (tenant_id, target_id)`, `chk_epoch_fence_nonnegative CHECK (latest_fence >= 0)`.
5.  **`resource_reservation`** (Foundation table preserved for `b3_005_claim_payload` schema parity; not used by B1):
    *   Columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT`, `target_id VARCHAR NOT NULL`, `holder_incident_id UUID NOT NULL REFERENCES incident(id) ON DELETE CASCADE`, `action_digest VARCHAR NOT NULL`, `fence BIGINT NOT NULL`, `expires_at TIMESTAMPTZ NOT NULL`, `state VARCHAR NOT NULL DEFAULT 'RESERVED'`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
    *   Constraints: `chk_reservation_fence_positive CHECK (fence > 0)`, `fk_reservation_incident_tenant FOREIGN KEY (tenant_id, holder_incident_id) REFERENCES incident(tenant_id, id) ON DELETE CASCADE`.
    *   Indexes: `ux_active_reservation_per_target UNIQUE (tenant_id, target_id) WHERE (state = 'RESERVED')`, `ix_reservation_active_expiry (expires_at) WHERE (state = 'RESERVED')`, `ix_reservation_tenant_target_fence (tenant_id, target_id, fence)`.
6.  **`effect_receipt`** (Foundation table preserved for `b3_005_claim_payload` schema parity; not used by B1):
    *   Columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT`, `incident_id UUID NOT NULL REFERENCES incident(id) ON DELETE CASCADE`, `plan_digest VARCHAR NOT NULL`, `target_id VARCHAR NOT NULL`, `adapter_id VARCHAR NOT NULL`, `idempotency_key VARCHAR NOT NULL`, `fence BIGINT NOT NULL`, `expected_version VARCHAR NOT NULL`, `state VARCHAR NOT NULL DEFAULT 'PREPARED'`, `attempt_count INTEGER NOT NULL DEFAULT 0`, `external_ref VARCHAR NULL`, `observed_version VARCHAR NULL`, `lease_owner VARCHAR NULL`, `lease_expires_at TIMESTAMPTZ NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `action_digest VARCHAR NOT NULL DEFAULT ''`, `claim_token UUID NULL`, `action_payload JSON NULL`.
    *   Constraints: `ux_effect_tenant_idempotency UNIQUE (tenant_id, idempotency_key)`, `fk_receipt_incident_tenant FOREIGN KEY (tenant_id, incident_id) REFERENCES incident(tenant_id, id) ON DELETE CASCADE`.
    *   Indexes: `ix_effect_receipt_pending_recon (state, updated_at) WHERE (state IN ('UNKNOWN', 'SENT'))`, `ix_effect_receipt_history (tenant_id, incident_id, created_at)`.

### Target Files to Create/Modify

#### 1. `backend/persistence/models.py` (Modify)
*   **Type / Scope**: SQLAlchemy 2.0 Declarative ORM Models
*   **Proposed Changes**: Define `Tenant`, `Incident`, `Outbox`, `ResourceEpoch`, `ResourceReservation`, and `EffectReceipt` matching the exact schema specification above.

#### 2. `backend/persistence/alembic/versions/0001_b3_005_claim_payload.py` (Create)
*   **Type / Scope**: Alembic Migration Revision (`revision = "b3_005_claim_payload"`, `down_revision = None`)
*   **Proposed Changes**: Reconstruct the idempotent baseline migration creating all 6 tables, constraints, and indexes so `alembic current` and fresh isolated schema upgrades succeed cleanly.

#### 3. `backend/persistence/alembic/env.py` (Modify)
*   **Type / Scope**: Alembic Environment Configuration
*   **Proposed Changes**: Respect `config.get_main_option("sqlalchemy.url")` when explicitly set and support `version_table_schema` / `include_schemas` for isolated schema migration tests.

#### 4. `backend/domain/__init__.py` & `backend/domain/incident.py` (Create)
*   **Type / Scope**: Domain State Machine, Canonical Hashing, and Pydantic Contracts
*   **Proposed Changes**:
    *   `IncidentStatus` (`StrEnum`): `ACCEPTED`, `DISPATCH_PENDING`, `INVESTIGATING`, `AWAITING_APPROVAL`, `EXECUTING`, `VERIFYING`, `VERIFIED`, `FAILED`, `NEEDS_REVIEW`, `CANCELLED`.
    *   `IncidentCreateRequest`: `target_id: str` (min_length=1, max_length=128, stripped), `title: str | None = None`, `severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] = "SEV2"`, `metadata: dict[str, Any] | None = None` (`extra="forbid"`).
    *   `IncidentResponse`: `id: UUID`, `tenant_id: UUID`, `client_key: str`, `target_id: str`, `request_sha256: str`, `status: IncidentStatus`, `status_version: int`, `workflow_id: str`, `created_at: datetime`, `outbox_delivered: bool`, `outbox_attempts: int`, `projection_lag_ms: float`, `observations: list[InvestigationObservation] | None = None`.
    *   `IncidentCancelRequest`: `expected_version: int | None = None`, `reason: str | None = None`.
    *   `canonical_request_hash(payload: dict[str, Any]) -> str`: Recursive NFC string normalization, omission of `None`-valued optional fields so missing and `null` optional fields normalize deterministically, sorted JSON keys, and SHA-256 hex digest.

#### 5. `backend/observability/__init__.py` & `backend/observability/metrics.py` (Create)
*   **Type / Scope**: Structured Telemetry, Secret Redaction, and Metrics Registry
*   **Proposed Changes**: Emit structured events (`incident.admitted`, `outbox.claimed`, `outbox.delivered`, `outbox.retry`, `outbox.stuck`, `workflow.started`, `workflow.completed`, `workflow.failed`, `workflow.cancelled`, `activity.retry`), redact secrets, and record metrics without unbounded tenant labels.

#### 6. `backend/persistence/db.py` (Modify) & `backend/persistence/repositories.py` (Create)
*   **Type / Scope**: Database Engine, Session Factory, and Tenant-Scoped Async Repositories
*   **Proposed Changes**: Add bounded connection pool configuration in `make_engine` and implement `IncidentRepository` and `OutboxRepository` with short transactions and `FOR UPDATE SKIP LOCKED` claims.

#### 7. `backend/investigator/__init__.py` & `backend/investigator/fake.py` (Create)
*   **Type / Scope**: Deterministic Fake Read-Only Investigator
*   **Proposed Changes**: Implement `FakeInvestigator` returning deterministic fixture observations based on `(tenant_id, incident_id, target_id)` with zero external I/O or mutation.

#### 8. `backend/workflows/__init__.py`, `backend/workflows/activities.py`, `backend/workflows/incident_workflow.py`, `backend/workflows/dispatcher.py`, `backend/workflows/worker.py` (Create)
*   **Type / Scope**: Temporal Workflow, Activities, Worker, and Outbox Dispatcher
*   **Proposed Changes**: Implement `IncidentWorkflow`, `project_incident_status_activity`, `run_fake_investigation_activity`, `OutboxDispatcher`, and worker factory.

#### 9. `backend/api/incidents.py` & `backend/api/main.py` (Create / Modify)
*   **Type / Scope**: FastAPI Routes, Middleware, and Health/Readiness Probes
*   **Proposed Changes**: Mount `/v1/incidents` endpoints, correlation ID middleware, secret-safe exception handlers, `/healthz`, `/readyz`, and `/metrics`.

#### 10. `frontend/src/App.tsx`, `frontend/src/App.css`, `frontend/vite.config.ts` (Modify)
*   **Type / Scope**: Minimal React + TypeScript B1 Status Interface
*   **Proposed Changes**: Build the B1 incident creation, reload-safe status display, refresh, and cancellation UI backed by FastAPI.

#### 11. `tests/conftest.py`, `tests/unit/test_b1_state_machine.py`, `tests/integration/test_b1_api_idempotency.py`, `tests/integration/test_b1_db_failures.py`, `tests/integration/test_b1_dispatcher_crash.py`, `tests/integration/test_b1_temporal_workflow.py`, `tests/security/test_b1_security.py`, `scripts/b1_perf_stress.py`, `docs/runbooks/b1-durable-incident-workflow.md`, `docs/evidence/b1-acceptance-report.md` (Create)
*   **Type / Scope**: Complete B1 Adversarial Test Suite, Performance Benchmark, Runbooks, and Acceptance Evidence Report.

### Incremental Execution Waves (GSD Plan)

*   **Wave 1 — Persistence, Migration Recovery & Domain Core**: `backend/persistence/models.py`, `backend/persistence/alembic/versions/0001_b3_005_claim_payload.py`, `backend/persistence/alembic/env.py`, `backend/domain/incident.py`, `backend/observability/metrics.py`. Checkpoint: `alembic current` and unit tests pass.
*   **Wave 2 — Repositories, Fake Investigator, Dispatcher & Temporal Workflow**: `backend/persistence/db.py`, `backend/persistence/repositories.py`, `backend/investigator/fake.py`, `backend/workflows/*`. Checkpoint: Repository and Temporal workflow tests pass.
*   **Wave 3 — FastAPI Endpoints & React Status UI**: `backend/api/incidents.py`, `backend/api/main.py`, `frontend/src/App.tsx`, `frontend/src/App.css`, `frontend/vite.config.ts`. Checkpoint: API integration tests and `npm --prefix frontend run build` pass.
*   **Wave 4 — Adversarial Testing, Performance Stress, Graphify & Evidence**: Full Omega test suite (`tests/`), `scripts/b1_perf_stress.py`, `docs/runbooks/b1-durable-incident-workflow.md`, `docs/evidence/b1-acceptance-report.md`, Graphify update, and EGM drift report.

### Verification Plan

*   **Compilation/Build**: `npm --prefix frontend run build` (`tsc -b && vite build`)
*   **Unit & Integration Tests**: `uv run pytest -v`
*   **Static Analysis & Linting**: `uv run ruff check backend tests scripts` and `uv run mypy`
*   **Database Migration Check**: `uv run alembic current` and isolated schema migration upgrade/downgrade test
*   **E2E/Performance**: `uv run python scripts/b1_perf_stress.py`

--------------------------------------------------------------------------------

*Drafted with EGM*
