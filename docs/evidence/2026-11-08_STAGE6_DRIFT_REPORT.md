# Architecture-to-Implementation Drift Report — TITAN-20 Stage 6

* **Date:** 2026-11-08
* **Specification References:**
  * [`docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md)
  * [`AGENTS.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/AGENTS.md)
  * TITAN-20 Master Production Architecture & Agentic AI Engineering Blueprint
* **Scope:**
  * **P2** — Version-Bound Approval Cockpit
  * **P1** — Live Investigation Workbench
  * **P3** — Evidence-to-Effect Trace Explorer
  * **S1** — Time-Sealed Paired Decision Replay & Statistical Evaluation Engine
  * **Frontend UI Extensions** — Workbench, Cockpit, Trace Explorer, Stale Lockout Banner
* **Review Methodology:** Elephant-Goldfish Model (`egm-drift-report` + Goldfish Subagents) + Superpowers / Addy Osmani 5-Axis Quality Audit.

---

## 1. Executive Summary

We performed an exhaustive audit comparing the implemented Stage 6 codebase, database schema, domain models, services, API routes, React/TypeScript components, and adversarial chaos test suites against [`ADR-0007`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0007-stage6-operator-control-plane-and-evaluation-engine.md).

All architectural requirements, non-negotiable security boundaries, role gates, database constraints, and EGM Goldfish review rulings have been implemented with **ZERO ARCHITECTURAL DRIFT**.

---

## 2. Specification vs. Implementation Comparison Matrix

### 2.1 Feature P2 — Version-Bound Approval Cockpit

| Specification Requirement (`ADR-0007`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Exact Plan Digest & Version Binding (`INV-P2-1`)** | Bound to `(tenant_id, incident_id, step_id, plan_id, canonical_plan_digest, action_digest, target_id, expected_resource_version, policy_version)`. Stale context/target version drift invalidates approval. | [`backend/domain/approval.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/domain/approval.py), [`backend/approvals/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/approvals/service.py) | **None (Exact Match)** | `test_p2_approval_cockpit.py::test_p2_stale_incident_context_fencing`, `test_stage6_omega_adversarial.py::test_adversarial_1_target_version_drift_invalidates_approval`. |
| **Server-Side Human Role Gating (`INV-P2-2`)** | Explicit role allowlist (`ALLOWED_APPROVAL_ROLES`). Denies autonomous models, agents, or unlisted roles with fail-closed HTTP 403 Forbidden. | [`backend/domain/approval.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/domain/approval.py), [`backend/approvals/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/approvals/service.py) | **None (Exact Match)** | `test_p2_approval_cockpit.py::test_p2_reviewer_role_authorization_rejects_model`, `test_stage6_omega_adversarial.py::test_adversarial_2_model_and_agent_self_approval_rejected_sever_side`. |
| **Two-Phase Dispatch Lease (`INV-P2-3`)** | `APPROVED -> DISPATCHING(lease_id, expires_at) -> CONSUMED`. Short database transactions before external execution. Stale zombie workers rejected if lease expires. | [`backend/approvals/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/approvals/service.py) (`claim_dispatch_lease`, `finalize_consumed`) | **None (Exact Match)** | `test_p2_approval_cockpit.py::test_p2_dispatch_lease_two_phase_claiming_and_consumption`, `test_stage6_omega_adversarial.py::test_adversarial_3_dispatch_lease_expiry_and_zombie_worker_rejection`. |
| **Active Approval Constraint & Concurrency** | Partial unique index `ux_active_approval_per_incident` in PostgreSQL ensures at most one active request. `SELECT ... FOR UPDATE` serializes concurrent decisions. | [`backend/persistence/models.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/models.py), Alembic migration `0004_stage6_p2_p3_s1.py` | **None (Exact Match)** | `test_stage6_omega_adversarial.py::test_adversarial_5_concurrent_approve_and_reject_serialization`. |

---

### 2.2 Feature P1 — Live Investigation Workbench

| Specification Requirement (`ADR-0007`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Authoritative REST Snapshots with ETag (`INV-P1-1`)** | `GET /v1/incidents/{id}/workbench` calculates SHA-256 ETag across status version, steps count, evidence count, tool-calls count, and active approval state. Client `If-None-Match` returns `304 Not Modified`. | [`backend/workbench/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workbench/service.py), [`backend/api/workbench.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/api/workbench.py) | **None (Exact Match)** | `test_p1_workbench.py::test_p1_workbench_snapshot_empty_and_populated`. |
| **Lightweight SSE Stream & Heartbeats** | `GET /v1/incidents/{id}/events` streams event notifications with monotonic IDs and periodic keepalive comments `": keepalive"` every 10s. | [`backend/api/workbench.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/api/workbench.py) (`stream_incident_events`) | **None (Exact Match)** | `test_p1_workbench.py::test_p1_workbench_sse_stream_events_and_heartbeats`. |
| **Stale-View Lockout Guard (`INV-P1-2`)** | Client monitors SSE ping arrival; if ping lag > 30s or disconnected, displays prominent lockout banner and disables mutation triggers. | [`frontend/src/components/StaleLockoutBanner.tsx`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/frontend/src/components/StaleLockoutBanner.tsx), [`frontend/src/components/WorkbenchView.tsx`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/frontend/src/components/WorkbenchView.tsx) | **None (Exact Match)** | Verified in TypeScript compilation and component inspection. |
| **Abstention & Budget Headroom Visibility** | Projects full hypothesis belief distribution, active tool-call timeline, budget remaining vs reserved, and detailed abstention reasons. | [`backend/workbench/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workbench/service.py), [`frontend/src/components/WorkbenchView.tsx`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/frontend/src/components/WorkbenchView.tsx) | **None (Exact Match)** | `test_p1_workbench.py::test_p1_workbench_abstention_detection`. |

---

### 2.3 Feature P3 — Evidence-to-Effect Trace Explorer

| Specification Requirement (`ADR-0007`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Authoritative DAG Lineage (`INV-P3-1`)** | Directly queries PostgreSQL tables (`incident`, `investigation_step`, `evidence_ledger`, `tool_call`, `approval_request`, `effect_receipt`) to build causal graph without synthetic edges. | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) (`TraceService.get_trace_graph`) | **None (Exact Match)** | `test_p3_trace_explorer.py::test_p3_trace_graph_dag_projection_and_acyclicity`. |
| **Cycle Detection (Acyclicity Invariant)** | Kahn's algorithm validates that the projected trace graph is a Directed Acyclic Graph (DAG) before returning. | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) (`_detect_cycles`) | **None (Exact Match)** | `test_p3_trace_explorer.py::test_p3_trace_graph_dag_projection_and_acyclicity`. |
| **Honest Missing Link Representation (`INV-P3-2`)** | Identifies unlinked evidence without downstream hypotheses or effects without approved requests and annotates them with clear warnings. | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) (`_identify_missing_links`) | **None (Exact Match)** | `test_p3_trace_explorer.py::test_p3_trace_graph_missing_link_annotations`. |
| **Recursive Secret Redaction** | All node metadata and payloads pass through recursive redaction (`redact_value`) scrubbing tokens, keys, and passwords before transmission. | [`backend/trace/service.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/trace/service.py) | **None (Exact Match)** | Unit tests and trace explorer view inspection. |

---

### 2.4 Feature S1 — Time-Sealed Paired Decision Replay & Statistical Evaluation Engine

| Specification Requirement (`ADR-0007`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Immutable Evaluation Manifests (`INV-S1-1`)** | Frozen manifests with canonical SHA-256 hash stored in `evaluation_manifest`. Trials persisted to `evaluation_trial` with composite unique constraint `(manifest_id, pair_id, strategy_name)`. | [`backend/persistence/models.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/models.py), [`backend/experiments/s1_replay.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/experiments/s1_replay.py) | **None (Exact Match)** | `test_s1_decision_replay.py::test_s1_time_sealed_decision_replay_harness_execution`. |
| **Protected Holdout Label Isolation (`INV-S1-2`)** | Ground-truth root causes encapsulated in `HiddenLabelStore` and `IndependentOracle`, strictly inaccessible to planner models during test runs. | [`backend/experiments/oracle.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/experiments/oracle.py) | **None (Exact Match)** | `test_s1_decision_replay.py::test_s1_independent_oracle_adjudication`. |
| **100% Negative Control Noise Immunity (`INV-S1-3`)** | Transient network spikes and expected failovers must result in `NO_OP`. Any mutation on a negative control fails the quality gate immediately. | [`backend/experiments/s1_replay.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/experiments/s1_replay.py), [`scripts/stage6_evaluation_benchmark.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/scripts/stage6_evaluation_benchmark.py) | **None (Exact Match)** | `test_stage6_omega_adversarial.py::test_adversarial_4_s1_negative_control_noisy_telemetry_immunity`. |
| **Standard-Library Statistical Tests (`INV-S1-4`)** | McNemar paired test (exact binomial / continuity-corrected $\chi^2$) and Wilcoxon signed-rank test implemented with zero heavy statistical dependencies. | [`backend/experiments/statistical_tests.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/experiments/statistical_tests.py) | **None (Exact Match)** | `test_s1_decision_replay.py::test_s1_mcnemar_statistical_test_properties`, `test_s1_wilcoxon_signed_rank_test_properties`. |

---

## 3. Goldfish Adversarial Rulings Compliance

| Review Ruling ID | Description | Resolution in Code | Status |
|---|---|---|---|
| **`[CRITICAL-1]`** | No database transaction held during external adapter mutation dispatch. | Implemented via two-phase lease: `claim_dispatch_lease()` runs in short transaction (<10ms), returns lease token. Adapter mutation runs without holding open database lock. `finalize_consumed()` runs in second short transaction. | **COMPLIANT** |
| **`[CRITICAL-2]`** | Reviewer role validation must enforce explicit human allowlist. | Implemented via `ALLOWED_APPROVAL_ROLES`. Autonomous agents, LLMs, and unlisted roles rejected with HTTP 403 Forbidden. | **COMPLIANT** |
| **`[CRITICAL-3]`** | Client-side stale views must lock out mutation triggers. | Implemented via `StaleLockoutBanner.tsx` and server-side status version validation (`expected_resource_version`). Stale submissions return HTTP 409 Conflict. | **COMPLIANT** |
| **`[CRITICAL-4]`** | Statistical tests must avoid heavy dependencies and implement standard formulations. | Implemented in `backend/experiments/statistical_tests.py` using Python's `math` standard library (`math.comb`, `math.erfc`). | **COMPLIANT** |
| **`[IMPORTANT-1]`** | Multi-tenant composite foreign keys must cascade cleanly across all 4 new tables. | Implemented in Alembic migration `0004_stage6_p2_p3_s1.py` with `ForeignKeyConstraint(["tenant_id", "incident_id"], ["incident.tenant_id", "incident.id"], ondelete="CASCADE")`. | **COMPLIANT** |
| **`[IMPORTANT-2]`** | Trace Explorer must honestly annotate missing links and cycles. | Implemented in `TraceService._identify_missing_links()` and `_detect_cycles()` using Kahn's topological sort algorithm. | **COMPLIANT** |

---

## 4. Empirical Benchmark Summary

From [`docs/evidence/stage6-evaluation-benchmark.json`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/evidence/stage6-evaluation-benchmark.json):

* **Evaluated Dataset:** 25 paired diagnostic fixtures (75 total trials in PostgreSQL).
* **Negative Control Pass Rate:** 100.0% (Zero false positives on benign incidents).
* **Strategy C Accuracy:** 100.0% (Wilson Score 95% CI: [86.7%, 100.0%]) vs Strategy A (60.0%, 95% CI: [40.7%, 76.6%]) and Strategy B (40.0%, 95% CI: [23.4%, 59.3%]).
* **McNemar Paired Test:** $p = 0.001953$ (vs Strategy A), $p = 0.000061$ (vs Strategy B) — statistically significant at $p < 0.01$.
* **Wilcoxon Signed-Rank Test:** $p = 0.000727$ (vs Strategy A) — statistically significant cost reduction at $p < 0.001$.
