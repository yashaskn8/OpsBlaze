# OpsBlaze / TITAN-20 Stage 6 Acceptance & Verification Report
## OPERATOR CONTROL PLANE, APPROVAL SYSTEM, TRACE EXPLORER & EVALUATION ENGINE

**Date:** 2026-11-08  
**Status:** COMPLETED — ALL 20 ACCEPTANCE GATES VERIFIED  
**Governing Documents:** Root [`AGENTS.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/AGENTS.md), [`ADR-0007`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md), TITAN-20 Master Production Architecture & Agentic AI Engineering Blueprint.  
**Tested Stack:** Windows 11 (native), Python 3.14.7, PostgreSQL 17.11 on `localhost:5432`, Temporal dev server (SQLite backend), SQLAlchemy Async (asyncpg), Alembic, Pytest 9.2.0, React 19, TypeScript 5.8, Vite 6.2.

---

## 1. Executive Summary

Stage 6 completes the operator control plane, human authorization boundary, causal trace exploration, and paired statistical evaluation foundation for OpsBlaze.

### Key Capabilities Delivered
1. **P2 — Version-Bound Approval Cockpit:** Strict human role gating (`ALLOWED_APPROVAL_ROLES`), cryptographic version fencing (`target_resource_version`, `status_version_at_creation`, `plan_digest`, `action_digest`), and a two-phase dispatch lease protocol (`APPROVED -> DISPATCHING -> CONSUMED`) that prevents double execution, handles worker crashes, and rejects zombie workers.
2. **P1 — Live Investigation Workbench:** Modern React + TypeScript operator workbench with deterministic ETag caching (`If-None-Match` -> `304 Not Modified`), Server-Sent Events (SSE) streaming with 10s keepalive pings, and an automatic stale-view lockout banner (>30s ping lag or disconnect) preventing actions on out-of-sync telemetry.
3. **P3 — Evidence-to-Effect Trace Explorer:** Direct PostgreSQL DAG projection tracing immutable lineage from initial alerts and symptoms through tool-call observations, hypothesis updates, evidence ledgers, human approvals, and reconciled effect receipts without synthetic edges. Incorporates Kahn's algorithm cycle detection, missing-link annotations, and recursive secret redaction.
4. **S1 — Time-Sealed Paired Decision Replay & Statistical Evaluation Engine:** Rigorous offline evaluation engine executing frozen candidate strategies against baselines on identical diagnostic fixtures. Features independent oracle adjudication (`IndependentOracle`), standard-library McNemar paired accuracy test ($p < 0.01$), Wilcoxon signed-rank cost test ($p < 0.001$), and a 100% negative control noise immunity gate.

All historical database data across 22 tenants, 22 incidents, and 29 pre-Stage-3 effect receipts remain 100% intact in `opsblaze_test`. The entire test suite of 130 tests passes green with zero errors, zero warnings, and zero type discrepancies.

---

## 2. Acceptance Gates Verification Matrix (G1 – G20)

| Gate | Requirement | Verification Method | Status | Evidence Link |
|---|---|---|---|---|
| **G1** | Baseline & Schema Integrity | Full verification of schema migration `stage6_001_p2_p3_s1` on `opsblaze` & `opsblaze_test` | **PASSED** | [`backend/persistence/migrations/versions/0004_stage6_p2_p3_s1.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/migrations/versions/0004_stage6_p2_p3_s1.py) |
| **G2** | Historical Data Safety | 22 historical tenants, 22 historical incidents, and 29 effect receipts preserved without data loss | **PASSED** | Verified via PostgreSQL count query in migration test run |
| **G3** | P2 Human Role Gating | Explicit allowlist verification; autonomous agents and LLMs rejected with HTTP 403 Forbidden | **PASSED** | [`test_p2_approval_cockpit.py::test_p2_reviewer_role_authorization_rejects_model`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p2_approval_cockpit.py) |
| **G4** | P2 Version Fencing | Target version drift or incident status advance invalidates approval request | **PASSED** | [`test_p2_approval_cockpit.py::test_p2_stale_incident_context_fencing`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p2_approval_cockpit.py) |
| **G5** | P2 Two-Phase Dispatch Lease | `APPROVED -> DISPATCHING(lease_id) -> CONSUMED` prevents double execution | **PASSED** | [`test_p2_approval_cockpit.py::test_p2_dispatch_lease_two_phase_claiming_and_consumption`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p2_approval_cockpit.py) |
| **G6** | P2 Zombie Worker Rejection | Worker with expired dispatch lease rejected from finalizing consumption | **PASSED** | [`test_stage6_omega_adversarial.py::test_adversarial_3_dispatch_lease_expiry_and_zombie_worker_rejection`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage6_omega_adversarial.py) |
| **G7** | P2 Concurrent Decision Serialization | `SELECT ... FOR UPDATE` serializes concurrent approve/reject decisions | **PASSED** | [`test_stage6_omega_adversarial.py::test_adversarial_5_concurrent_approve_and_reject_serialization`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage6_omega_adversarial.py) |
| **G8** | P1 REST ETag Caching | Server issues ETag; client `If-None-Match` returns `304 Not Modified` when unchanged | **PASSED** | [`test_p1_workbench.py::test_p1_workbench_snapshot_empty_and_populated`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p1_workbench.py) |
| **G9** | P1 SSE Stream & Heartbeats | SSE stream yields events and periodic keepalive comments `": keepalive"` | **PASSED** | [`test_p1_workbench.py::test_p1_workbench_sse_stream_events_and_heartbeats`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p1_workbench.py) |
| **G10** | P1 Abstention Detection | Investigator abstention reasons properly projected into workbench payload | **PASSED** | [`test_p1_workbench.py::test_p1_workbench_abstention_detection`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p1_workbench.py) |
| **G11** | P1 Stale Lockout Safety | UI disables mutation triggers and shows lockout banner when SSE lag > 30s | **PASSED** | [`frontend/src/components/StaleLockoutBanner.tsx`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/frontend/src/components/StaleLockoutBanner.tsx), verified in frontend build |
| **G12** | P3 Causal DAG Projection | Authoritative DAG projection across tools, evidence, hypotheses, approvals, effects | **PASSED** | [`test_p3_trace_explorer.py::test_p3_trace_graph_dag_projection_and_acyclicity`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p3_trace_explorer.py) |
| **G13** | P3 Cycle Detection | Kahn's algorithm validates acyclicity; cycles rejected with error | **PASSED** | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) (`TraceService._detect_cycles`) |
| **G14** | P3 Missing-Link Annotations | Identifies unlinked evidence and actions without approval with warnings | **PASSED** | [`test_p3_trace_explorer.py::test_p3_trace_graph_missing_link_annotations`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_p3_trace_explorer.py) |
| **G15** | P3 Secret Redaction | Credentials and bearer tokens recursively scrubbed from all node payloads | **PASSED** | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) (`TraceService._build_node`) |
| **G16** | S1 Time-Sealed Replay | Immutable `evaluation_manifest` seals test fixtures and parameters | **PASSED** | [`test_s1_decision_replay.py::test_s1_time_sealed_decision_replay_harness_execution`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_s1_decision_replay.py) |
| **G17** | S1 Independent Oracle | Compares actions against hidden ground truth labels without leakage | **PASSED** | [`test_s1_decision_replay.py::test_s1_independent_oracle_adjudication`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_s1_decision_replay.py) |
| **G18** | S1 Negative Control Gate | 100% pass rate requirement on benign incidents (0% false-alarm tolerance) | **PASSED** | [`test_stage6_omega_adversarial.py::test_adversarial_4_s1_negative_control_noisy_telemetry_immunity`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage6_omega_adversarial.py) |
| **G19** | S1 Statistical Tests | Standard-library McNemar paired test and Wilcoxon signed-rank test | **PASSED** | [`test_s1_decision_replay.py::test_s1_mcnemar_statistical_test_properties`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_s1_decision_replay.py) |
| **G20** | Full Integration & Build | Frontend TypeScript compilation, backend linter, type checker, end-to-end suite | **PASSED** | 130 tests passed (60.28s), `ruff` clean, `mypy` clean (59 files), `npm run build` clean (273ms) |

---

## 3. Statistical Evaluation & Replay Benchmark Results

Derived from execution of `scripts/stage6_evaluation_benchmark.py` (recorded in [`docs/evidence/stage6-evaluation-benchmark.json`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/evidence/stage6-evaluation-benchmark.json)):

- **Manifest ID:** `42873c21-6d87-4963-8c04-aab61a294fba`
- **Total Evaluated Pairs:** 25 pairs (75 individual trials in PostgreSQL)
- **Negative Control Noise Immunity:** 100.0% Pass Rate (0 false positives on transient faults)
- **Overall Quality Gate:** **PASSED**

### Strategy Performance Summary Table

| Strategy | Decision Accuracy | Wilson Score 95% CI | Average Cost (USD micros) | Average Probes | Relative Cost vs Strategy A |
|---|---|---|---|---|---|
| **Strategy A: Static Runbook** | 60.0% | [40.7%, 76.6%] | $11,500.0 | 2.40 | Baseline |
| **Strategy B: Cheapest-First Heuristic** | 40.0% | [23.4%, 59.3%] | $1,600.0 | 1.00 | -86.1% (Low Accuracy) |
| **Strategy C: A1 Adaptive Probe Planner** | **100.0%** | **[86.7%, 100.0%]** | **$7,700.0** | **1.40** | **-33.0% Cost, +40.0% Accuracy** |

### Hypothesis Testing (McNemar Paired Test for Accuracy vs Strategy C)
- **Strategy A vs Strategy C:**
  - Contingency table: $n_{00} = 0, n_{01} = 10, n_{10} = 0, n_{11} = 15$
  - Test Statistic: 10.0
  - $p\text{-value}$: **0.001953** (Statistically significant at $p < 0.01$)
- **Strategy B vs Strategy C:**
  - Contingency table: $n_{00} = 0, n_{01} = 15, n_{10} = 0, n_{11} = 10$
  - Test Statistic: 15.0
  - $p\text{-value}$: **0.000061** (Statistically significant at $p < 0.0001$)

### Cost Comparison (Wilcoxon Signed-Rank Test vs Strategy C)
- **Strategy A vs Strategy C:**
  - $W\text{-statistic} = 0.0, W^+ = 120.0, W^- = 0.0, z = -3.3794$
  - $p\text{-value}$: **0.000727** (Statistically significant cost reduction, $p < 0.001$)
- **Strategy B vs Strategy C:**
  - $p\text{-value}$: **0.000013** (Strategy B is cheaper but sacrifices 60% accuracy)

---

## 4. Adversarial Chaos Drill Findings

In [`tests/chaos/test_stage6_omega_adversarial.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage6_omega_adversarial.py), five critical failure drills were executed:

1. **Target Version Drift Invalidation:** A plan approved for target version `v1` is prevented from dispatching when the live target drifts to `v2`. The approval service returns HTTP 409 Conflict and halts mutation.
2. **Autonomous Model Self-Approval Injection Prevention:** A compromised or misbehaving agent sending `reviewer_role: "agent"` or `"model"` is rejected with HTTP 403 Forbidden. Only authorized human roles can trigger state changes.
3. **Dispatch Lease Expiry & Zombie Worker Rejection:** A worker that stalls or dies during execution loses its lease after TTL. A retry worker reclaims the lease; when the original zombie worker attempts to finalize, it is rejected with HTTP 409 Conflict.
4. **S1 Negative Control Noise Immunity:** Transient network spikes, benign leader failovers, and minor packet jitter injected into the evaluation harness are evaluated as benign NO-OPs by Strategy C, avoiding expensive or disruptive service restarts.
5. **Concurrent Approve/Reject Serialization:** When two operators submit contradictory decisions (`APPROVE` and `REJECT`) simultaneously, PostgreSQL `SELECT ... FOR UPDATE` serializes the transaction, granting victory to the first committer and returning a conflict error to the second.
