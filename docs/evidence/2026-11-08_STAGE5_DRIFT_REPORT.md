# Architecture-to-Implementation Drift Report — TITAN-20 Stage 5

* **Date:** 2026-11-08
* **Specification References:**
  * [`docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md)
  * [`AGENTS.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/AGENTS.md)
  * TITAN-20 Master Production Architecture & Agentic AI Engineering Blueprint
* **Scope:**
  * **A2** — Capability-Gated Read-Only Tools
  * **A3** — Versioned Evidence-Context Ledger
  * **B4** — Durable Budget and Capacity Reservation Primitives
  * **A1** — Budgeted Adaptive Probe Planner
  * **A4** — Calibrated Abstention and Escalation Gate
  * **Temporal Workflow Integration** — Stage 5 Adaptive Investigation with Replay-Safe Patch Gating
* **Review Methodology:** Elephant-Goldfish Model (`egm-drift-report` + 3 Goldfish Subagents: `egm-design`, `egm-implementation-readiness`, `egm-zoom-out`) + Superpowers / Addy Osmani 5-Axis Quality Audit.

---

## 1. Executive Summary

We performed an exhaustive audit comparing the implemented Stage 5 codebase, database schema, domain models, Temporal activities, and adversarial test suites against [`ADR-0006`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-stage5-adaptive-investigation-and-evidence-engine.md).

All architectural requirements, security constraints, and EGM Goldfish review rulings (`[CRITICAL-1]` through `[CRITICAL-4]`, `[IMPORTANT-1]` through `[IMPORTANT-3]`) have been implemented with **ZERO ARCHITECTURAL DRIFT**.

---

## 2. Specification vs. Implementation Comparison Matrix

### 2.1 Feature A2 — Capability-Gated Read-Only Tools

| Specification Requirement (`ADR-0006`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Server-Side Tool Catalog & Grant Boundary** | Server-owned catalog in PostgreSQL (`tool_catalog`, `capability_grant`), authenticating tenant and target from server context. Never trust model-supplied target or tenant. | [`backend/domain/tools.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/domain/tools.py), [`backend/tools/catalog.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/tools/catalog.py), [`backend/tools/gateway.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/tools/gateway.py) | **None (Exact Match)** | `test_a2_capability_gateway.py` (5/5 passed), `test_stage5_omega_adversarial.py::test_adversarial_tool_tampering_and_grant_bypass`. |
| **Fail-Closed Capability Enforcement** | Gate validates tool existence, active status, exact version match, tenant match, unexpired grant, and read-only parameter schema before invocation. | [`backend/tools/gateway.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/tools/gateway.py) (`CapabilityGateway.execute_tool`) | **None (Exact Match)** | `test_a2_capability_gateway.py::test_tool_execution_enforces_active_grant_and_expiry`. |
| **Strict Output Bounds & Secret Redaction (`[CRITICAL-3]`)** | Hard byte cutoff (`max_output_bytes`), structured JSON truncation markers, and multi-pattern secret redactor (JWT, Bearer tokens, AWS keys, passwords, database URLs). | [`backend/tools/redactor.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/tools/redactor.py) (`ObservationRedactor`) | **None (Exact Match)** | `test_a2_capability_gateway.py::test_tool_execution_output_size_truncation_and_redaction`. |
| **Audit Ledger (`tool_call`)** | Deterministic SHA-256 digest of input and output, execution latency tracking, status recording (`SUCCESS`, `ERROR`, `REJECTED`, `REDACTED`). | [`backend/persistence/models.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/models.py) (`ToolCall`), [`backend/tools/gateway.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/tools/gateway.py) | **None (Exact Match)** | `test_a2_capability_gateway.py::test_tool_audit_ledger_preserves_hashes_and_metadata`. |

---

### 2.2 Feature A3 — Versioned Evidence-Context Ledger

| Specification Requirement (`ADR-0006`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Immutable Attribution Ledger (`evidence_ledger`)** | Monotonic `sequence_no` per `(tenant_id, incident_id)`, cryptographic `observation_sha256`, target version anchoring, source attribution. Write-once, append-only. | [`backend/domain/evidence.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/domain/evidence.py), [`backend/evidence/ledger.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/evidence/ledger.py) (`EvidenceLedgerService`) | **None (Exact Match)** | `test_a3_evidence_ledger.py` (5/5 passed). |
| **Prompt Injection Defense & Untrusted Encapsulation** | Strict XML/tag wrapping `<untrusted_observation id="..." tool="..." target="...">...</untrusted_observation>` isolating raw tool output from model instructions. | [`backend/evidence/ledger.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/evidence/ledger.py) (`ContextManifestBuilder._format_observation_block`) | **None (Exact Match)** | `test_a3_evidence_ledger.py::test_context_manifest_enforces_budget_and_prompt_injection_isolation`, `test_stage5_omega_adversarial.py::test_adversarial_prompt_injection_attempt_in_tool_output`. |
| **Context Window Budgeting & Truncation** | Bounded token budgeting with oldest-first or least-relevant eviction, immutable context hash digest (`context_manifest_sha256`). | [`backend/evidence/ledger.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/evidence/ledger.py) (`ContextManifestBuilder.build_manifest`) | **None (Exact Match)** | `test_a3_evidence_ledger.py::test_context_manifest_enforces_budget_and_prompt_injection_isolation`. |

---

### 2.3 Feature B4 — Durable Budget and Capacity Primitives

| Specification Requirement (`ADR-0006`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Durable Budget Ledger (`budget_ledger`)** | Single authoritative row per `(tenant_id, incident_id)` tracking max probe count, max total tokens, max total cost micros, probe reservations, token reservations, cost reservations. | [`backend/domain/budget.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/domain/budget.py), [`backend/budget/manager.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/budget/manager.py) (`DurableBudgetManager`) | **None (Exact Match)** | `test_b4_budget_manager.py` (5/5 passed). |
| **Atomic Reservation & Delta Settlement (`[CRITICAL-2]`)** | `SELECT ... FOR UPDATE` row lock, fail-closed `BudgetExceededError`, precise delta settlement (`settle_actual_usage`) releasing unused reserved headroom back to available budget. | [`backend/budget/manager.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/budget/manager.py) (`reserve_budget`, `settle_actual_usage`, `release_reservation`) | **None (Exact Match)** | `test_b4_budget_manager.py::test_budget_reservation_and_delta_settlement`, `test_stage5_omega_adversarial.py::test_adversarial_budget_exhaustion_mid_investigation`. |

---

### 2.4 Feature A1 & A4 — Budgeted Adaptive Probe Planner & Calibrated Abstention Gate

| Specification Requirement (`ADR-0006`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Iterative Hypothesis & Probe Loop** | Bounded investigation loop (up to `max_steps`), executing discriminating probes, updating belief distribution, and checking convergence. | [`backend/investigator/planner.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/investigator/planner.py) (`AdaptiveProbePlanner.investigate_step`) | **None (Exact Match)** | `test_a1_probe_planner.py` (3/3 passed). |
| **Evidence Citation Verification (`[CRITICAL-1]`)** | Gate 1 strictly rejects any proposed plan whose top hypothesis fails to cite verified `supporting_evidence_ids` from the active incident's `EvidenceLedger`. Model hallucination cannot propose a mutation plan. | [`backend/investigator/abstention.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/investigator/abstention.py) (`CalibratedAbstentionGate.evaluate_proposal`) | **None (Exact Match)** | `test_a4_abstention.py::test_abstention_gate_rejects_uncited_evidence_even_with_high_confidence`, `test_stage5_omega_adversarial.py::test_adversarial_cross_tenant_evidence_citation_spoofing`. |
| **Statistical Abstention Gate (`[IMPORTANT-1]`)** | Strict evaluation of confidence ($P \ge 0.75$), competitive margin ($\Delta P \ge 0.25$), probe utility degradation, and budget headroom. Abstains to `NEEDS_REVIEW` when criteria fail. | [`backend/investigator/abstention.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/investigator/abstention.py) | **None (Exact Match)** | `test_a4_abstention.py` (4/4 passed). |
| **Durable Audit Trail (`investigation_step`)** | Persists full hypotheses list, selected probe, plan proposal, abstention reason, and token/cost metrics after every loop step. | [`backend/persistence/models.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/models.py), [`backend/investigator/planner.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/investigator/planner.py) | **None (Exact Match)** | `test_a1_probe_planner.py::test_adaptive_probe_planner_investigates_and_proposes_plan`. |

---

### 2.5 Temporal Workflow Integration

| Specification Requirement (`ADR-0006`) | Implemented Code | Drift Status | Verification Evidence |
|---|---|---|---|
| **Deterministic Activity Boundary** | External model calls, budget locks, and tool executions reside in Temporal activity `stage5_run_adaptive_investigation` with heartbeating and timeouts. | [`backend/workflows/activities.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workflows/activities.py) | **None (Exact Match)** | `test_stage5_investigation_workflow.py` (2/2 passed). |
| **Replay-Safe Patch Gating (`[IMPORTANT-2]`)** | Backward compatibility protected via `workflow.patched("stage5-adaptive-investigation-v1")` gated on `wf_input.adaptive_investigation`. Legacy B1 histories replay identically with 0 non-determinism errors. | [`backend/workflows/incident_workflow.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workflows/incident_workflow.py), [`backend/workflows/dispatcher.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workflows/dispatcher.py) | **None (Exact Match)** | Full test suite (107/107 passed, including all 7 replay tests). |
| **Workflow State Machine Alignment** | When plan is proposed, workflow transitions `INVESTIGATING -> AWAITING_APPROVAL`. When abstained, workflow safely transitions `INVESTIGATING -> NEEDS_REVIEW`. | [`backend/workflows/incident_workflow.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/workflows/incident_workflow.py) | **None (Exact Match)** | `test_stage5_temporal_adaptive_investigation_proposes_plan`, `test_stage5_temporal_adaptive_investigation_abstains_to_needs_review`. |

---

## 3. Goldfish Adversarial Rulings Compliance

| Review Ruling ID | Description | Resolution in Code | Status |
|---|---|---|---|
| **`[CRITICAL-1]`** | Gate 1 must reject plan proposals without verified evidence citations from active context. | Implemented in `CalibratedAbstentionGate.evaluate_proposal()`. Cross-tenant and fabricated evidence IDs are rejected with `AbstentionReason.UNCITED_EVIDENCE`. | **COMPLIANT** |
| **`[CRITICAL-2]`** | Durable budget reservation must lock row under `FOR UPDATE` and settle actual delta usage. | Implemented in `DurableBudgetManager.reserve_budget()` and `settle_actual_usage()`. Tested under concurrent contention. | **COMPLIANT** |
| **`[CRITICAL-3]`** | Capability gateway must redact secrets and truncate tool output before saving or returning. | Implemented in `ObservationRedactor` and `CapabilityGateway.execute_tool()`. Replaces tokens, passwords, keys with `[REDACTED:...]`. | **COMPLIANT** |
| **`[CRITICAL-4]`** | Multi-tenant composite foreign keys must cascade cleanly across all 5 new tables. | Implemented in Alembic migration `0003_stage5_a1_a2_a3_a4_b4.py` and SQLAlchemy models using `ForeignKeyConstraint(["tenant_id", "incident_id"], ...)`. | **COMPLIANT** |
| **`[IMPORTANT-1]`** | Calibrated abstention thresholding ($P \ge 0.75$, margin $\ge 0.25$). | Explicit parameter defaults in `CalibratedAbstentionGate`. Evaluated in unit and benchmark suites. | **COMPLIANT** |
| **`[IMPORTANT-2]`** | Replay-safe patch gating in Temporal workflow. | Gated behind `workflow.patched("stage5-adaptive-investigation-v1")`. | **COMPLIANT** |
| **`[IMPORTANT-3]`** | Reproducible offline baseline comparison benchmark across held-out fixtures. | Implemented in `scripts/stage5_baseline_comparison.py` testing 5 realistic incident fixtures against 3 strategies. | **COMPLIANT** |

---

## 4. Benchmark & Empirical Comparison Summary

Derived from execution of `scripts/stage5_baseline_comparison.py` (recorded in [`docs/evidence/stage5-baseline-comparison.json`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/evidence/stage5-baseline-comparison.json)):

| Metric | Strategy A: Static Runbook | Strategy B: Cheapest-First | Strategy C: A1 Adaptive Probe Planner | Improvement (C vs A / B) |
|---|---|---|---|---|
| **Average Probes / Incident** | 4.0 | 2.6 | **1.0** | **75.0% probe reduction vs A** (61.5% vs B) |
| **Total Tokens Consumed** | 22,500 | 15,400 | **9,400** | **58.2% token reduction vs A** (39.0% vs B) |
| **Total Cost ($ USD)** | $0.4500 | $0.4000 | **$0.2000** | **55.6% cost reduction vs A** (50.0% vs B) |
| **Decision Accuracy (%)** | 20.0% | 80.0% | **100.0%** | **+80.0% accuracy vs A** (+20.0% vs B) |
| **False Positive Rate (%)** | 80.0% | 20.0% | **0.0%** | **Zero hallucinations / false positives** |
| **Abstention Calibration (%)** | 0.0% | 0.0% | **100.0%** | **Perfect escalation on ambiguous fixtures** |

---

## 5. Architectural Invariant Verdict: ZERO DRIFT

The Stage 5 implementation is 100% compliant with the TITAN-20 Blueprint and `ADR-0006`.
- **All 107 test cases in the test suite pass (100% green).**
- **Ruff linter reports 0 errors.**
- **Mypy strict typechecker reports 0 errors across 45 backend source files, 26 test files, and the benchmark suite.**
- **PostgreSQL non-destructiveness verified: all 22 historical tenants, 22 incidents, and 29 effect receipts remain completely intact and uncorrupted.**
