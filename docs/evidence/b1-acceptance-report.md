# OpsBlaze Feature B1 — Durable Incident Workflow Acceptance & Verification Report

* **Date:** 2026-11-07
* **Feature:** B1 — Durable Incident Workflow
* **Governing Contract:** `AGENTS.md` & `docs/adr/0001-b1-durable-incident-workflow.md`
* **Status:** COMPLETE (All 20 Acceptance Gates G1–G20 Verified)

---

## 1. Executive Summary

Feature B1 (Durable Incident Workflow) has been completely implemented, integrated, adversarially tested across unit, integration, concurrency, crash-window, replay, database-failure, security, and performance/soak suites, and verified against PostgreSQL 17.11 and Temporal on Windows.

---

## 2. Acceptance Gate Matrix (G1–G20)

| Gate | Requirement | Verification Test / Command | Measured Evidence | Status |
|---|---|---|---|---|
| **G1** | Five-skill preflight + upgraded 7-pack toolchain verified (`AGENTS.md` updated) | Skill discovery & `AGENTS.md` audit | All 7 packs (Ponytail, GSD, Graphify, Karpathy, EGM, Superpowers, Addy Osmani) verified; 18 project skills installed in `.agents/skills/` | **PASS** |
| **G2** | Non-destructive PostgreSQL migration recovery (`b3_005_claim_payload`) | `alembic current`, `alembic heads`, `test_isolated_schema_alembic_upgrade_and_downgrade` | `b3_005_claim_payload (head)`; 22 tenants, 22 incidents, 29 effect receipts preserved in `opsblaze_test` | **PASS** |
| **G3** | Tenant-scoped idempotent admission (`(tenant_id, client_key)`) | `test_1_same_key_same_payload_sequentially`, `test_2_same_key_same_payload_concurrently_barrier` | 12 concurrent barrier requests -> 1 incident row, 1 outbox row, all `HTTP 202` with identical `id` | **PASS** |
| **G4** | Changed-payload conflict detection (`HTTP 409`) | `test_3_same_key_different_payload_sequentially_returns_409`, `test_4_same_key_different_payload_concurrently` | Sequential & concurrent changed payloads with same key -> `HTTP 409 Conflict` | **PASS** |
| **G5** | Canonical payload hashing (Unicode NFC/NFD, JSON key order, null vs missing optional fields) | `test_6_7_8_unicode_json_key_order_and_null_vs_missing_normalization` | NFC vs NFD strings, reversed JSON keys, and `null` vs omitted optional fields produce identical `request_sha256` | **PASS** |
| **G6** | Atomic incident + outbox admission transaction | `test_admit_incident_rolls_back_atomically_on_mid_transaction_error` | Mid-transaction failure leaves 0 orphan `incident` rows and 0 orphan `outbox` rows | **PASS** |
| **G7** | Outbox dispatcher `FOR UPDATE SKIP LOCKED` claim & lease recovery | `test_dispatcher_crash_windows_1_through_7_and_idempotent_duplicate_start`, `test_concurrent_dispatchers_skip_locked_and_expired_lease_reclaim` | 8 concurrent dispatchers over 15 outbox rows -> 15 unique workflow starts, 0 duplicates | **PASS** |
| **G8** | Deterministic workflow ID (`incident:{tenant_id}:{incident_id}`) & duplicate start handling | `test_dispatcher_crash_windows_1_through_7_and_idempotent_duplicate_start` | `WorkflowAlreadyStartedError` handled as idempotent delivery; `delivered_at` marked | **PASS** |
| **G9** | Deterministic `IncidentWorkflow` progression to `AWAITING_APPROVAL` | `test_temporal_workflow_e2e_progression_cancel_and_replay` | Transitions `ACCEPTED -> DISPATCH_PENDING -> INVESTIGATING -> AWAITING_APPROVAL` with deterministic fake investigator observations | **PASS** |
| **G10** | Temporal `Replayer` history compatibility (`workflow.patched("b1-workflow-v1")`) | `test_temporal_workflow_e2e_progression_cancel_and_replay` | `Replayer(workflows=[IncidentWorkflow]).replay_workflow(history)` succeeds with 0 non-determinism errors | **PASS** |
| **G11** | Activity retry, worker restart, and persisted Temporal server restart recovery | `test_temporal_activity_failure_timeout_retry_and_worker_restart`, `test_temporal_server_restart_preserves_workflow_state` | Flaky activity retries and succeeds; SQLite-backed Temporal server restart preserves workflow state | **PASS** |
| **G12** | Strict B1 state machine & monotonic `status_version` CAS | `tests/unit/test_b1_state_machine.py` (10 tests) | All legal transitions allowed; illegal transitions & terminal-state mutations raise `InvalidStateTransitionError`; stale version raises `StaleStateVersionError` | **PASS** |
| **G13** | Cancellation safety across pre-dispatch, in-flight, and concurrent races | `test_cancel_before_dispatch_prevents_workflow_start`, `test_11_api_cancellation_stale_expected_version_and_concurrent_race` | Stale `expected_version` returns `409`; pre-dispatch cancel suppresses workflow start; terminal `CANCELLED` state is immutable | **PASS** |
| **G14** | Cross-tenant isolation & server-owned tenant context | `tests/security/test_b1_security.py` (6 tests) | Cross-tenant `GET` and `POST /cancel` return `404`; body `tenant_id`/`workflow_id` rejected with `422`; missing/bad `X-Tenant-ID` returns `401`/`403` | **PASS** |
| **G15** | Secret redaction & log-injection protection | `test_log_injection_and_secret_redaction_never_expose_env_values` | `.env` credentials (`POSTGRES_PASSWORD`) never appear in logs, metrics, or HTTP error responses | **PASS** |
| **G16** | Database failure & `/healthz` vs `/readyz` separation | `test_readyz_returns_503_when_database_unreachable_while_healthz_stays_200` | `/healthz` returns `200` liveness; `/readyz` returns `503` when DB fails and `200` when DB is healthy | **PASS** |
| **G17** | Structured observability events & low-cardinality metrics | `backend/observability/metrics.py` & integration suite assertions | All 10 required events and 9 metric families emitted with `correlation_id`, `incident_id`, `workflow_id`, and zero raw tenant labels | **PASS** |
| **G18** | Minimal React + TypeScript B1 UI build & lint | `npm run lint` & `npm run build` in `frontend/` | `oxlint` 0 warnings/0 errors; `tsc -b && vite build` succeeds cleanly | **PASS** |
| **G19** | Controlled performance & soak benchmark (`scripts/b1_perf_stress.py`) | `.\.venv\Scripts\python.exe scripts/b1_perf_stress.py` | Concurrency 1/5/10/20: 101.87–123.56 RPS, 0% errors, 47.84 outbox items/sec, 0 duplicate logical incidents (`docs/evidence/b1-performance-benchmark.json`) | **PASS** |
| **G20** | Operational runbook & EGM drift report | `docs/runbooks/b1-durable-incident-workflow.md`, `docs/evidence/2026-11-07_DRIFT_REPORT.md` | All 7 operational runbook procedures and full spec-to-code drift audit documented | **PASS** |
