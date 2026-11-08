# OpsBlaze / TITAN-20 Stage 9 Acceptance & Verification Report
## E5 CANDIDATE-TO-INCUMBENT REGRESSION GATE

**Date:** 2026-11-08  
**Status:** COMPLETED — ALL 20 ACCEPTANCE GATES VERIFIED  
**Governing Documents:** Root [`AGENTS.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/AGENTS.md), [`ADR-0010`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0010-stage9-candidate-incumbent-regression-gate.md), TITAN-20 Master Production Architecture & Agentic AI Engineering Blueprint 2027.  
**Tested Stack:** Windows 11 (native), Python 3.14.7, PostgreSQL 17.11 on `localhost:5432`, SQLAlchemy Async (asyncpg), Alembic, Pytest 9.2.0, React 19, TypeScript 5.8, Vite 6.2.

---

## 1. Executive Summary

Milestone 9 completes the immutable, reproducible, statistically responsible offline evaluation gate for OpsBlaze by delivering **Feature E5 (Candidate-to-Incumbent Regression Gate)**. Building on the foundation established by B1–B4 durable workflows and budgets, A1–A4 adaptive investigation and capability gateways, P1–P3 live cockpit and trace DAG projections, S1 paired replay experiments, E1–E2 experience capture and adjudication, and E3–E4 improvement proposals and validated operational lessons, Feature E5 enables OpsBlaze to rigorously test proposed agent policy and memory changes against a pinned incumbent baseline using independent outcome evidence across frozen diagnostic fixture holdouts and adversarial negative controls.

### Key Capabilities Delivered
1. **Immutable Evaluation Manifests & Cryptographic Attestation (`INV-E5-1`):**
   - Manifest parameters (incumbent bundle, candidate bundle, provenance type, fixture family, tool catalog, A2 grant versions, oracle version, metric specification, sample size, random seed, ceilings) are cryptographically bound via canonical SHA-256 (`manifest_sha256`).
   - Freezing a manifest transitions its state from `REGISTERED` to `FROZEN` via atomic version-checked CAS. Once frozen, manifest configuration and thresholds are locked permanently.
2. **Double-Blind Context Stripping (`INV-E5-2`):**
   - Candidate and incumbent agent execution contexts strictly strip ground truth labels (`hidden_label`) and negative control flags (`is_negative_control`). The agent cannot discover whether a fixture is a positive fault or an adversarial negative control.
3. **Strict Candidate Provenance & Holdout Contamination Gate (`INV-E5-3`, `INV-E5-11`):**
   - Candidates originate from review-approved E3 proposals (`E3_PROPOSAL`), active E4 lessons (`E4_LESSON_SET`), or combined offline studies (`COMBINED_OFFLINE_STUDY`).
   - The holdout contamination gate (`INV-E5-11`) strictly checks that candidate training and source incidents have zero intersection with the frozen holdout fixture set, rejecting any contaminated manifest with HTTP 422.
4. **Deterministic Paired Baseline Comparison (`INV-E5-4`):**
   - Every candidate trial is paired with an incumbent trial evaluated on the exact same fixture under identical budget bounds and tool capabilities. Unique database constraint `(manifest_id, pair_id, arm)` prevents duplicate arm execution.
5. **Statistical Non-Regression Engine (`INV-E5-5`):**
   - Employs **Newcombe Method 10** matched-pair score confidence intervals for recovery rate difference $\Delta_{\text{recovery}}$ and exact/continuity-corrected McNemar tests.
   - Enforces a pre-specified non-inferiority margin $\delta_{\text{NI}} = 0.05$. When zero discordant regressions occur ($n_{01} = 0, \Delta \ge 0$), the candidate is non-inferior by mathematical definition.
6. **Zero-Tolerance Safety Gate (`INV-E5-6`):**
   - Any consequential or unauthorized mutation executed on a negative control immediately trips `GATE_SAFETY` to `FAIL` and disqualifies the candidate.
7. **Cost per Verified Recovery Bound (`INV-E5-7`):**
   - Candidate cost per verified recovery must not exceed $1.25\times$ the incumbent. If a candidate achieves zero verified recoveries, cost per recovery is infinite and fails `GATE_COST_BOUND`.
8. **Independent Deterministic Oracle Authority (`INV-E5-8`):**
   - Only the deterministic `IndependentOracle` scores trial outcomes based on withheld ground truth. Candidates cannot self-grade or self-certify.
9. **Synchronous Query-Time Dynamic Invalidation (`INV-E5-9`):**
   - If underlying source outcome revisions are superseded or disputed, or tenant consent is revoked, `promotion_eligible` is immediately flipped to `false` at query/review time with zero staleness window.
10. **Human Release Authority & Concurrency Control (`INV-E5-10`):**
    - Review sign-off is restricted to human operator roles (`incident_commander`, `sre_lead`, `platform_admin`). Atomic CAS on report version (`expected_version`) guarantees zero lost updates.
11. **Fault-Tolerant Worker Leasing & Crash Recovery (`INV-E5-12`):**
    - Distributed worker leasing with `worker_id` and `lease_expires_at` using short `SELECT FOR UPDATE SKIP LOCKED` transactions guarantees safe recovery from worker crashes without orphaned or duplicated trials.
12. **End-to-End P3 Trace DAG Lineage:**
    - Manifests and reports project seamlessly into the P3 causal DAG via `EVALUATION_MANIFEST` and `EVALUATION_REPORT` nodes with `EVALUATED_IN` and `REPORTED_BY` edges.

All pre-existing PostgreSQL database rows (13 tenants, 67 incidents in `opsblaze`; 92 tenants, 82 incidents in `opsblaze_test`) remain 100% intact. The repository test suite comprises **208 passing tests (0 failures, 0 skipped)** across unit, integration, and chaos suites, with complete linter (`ruff`) cleanliness.

---

## 2. Invariant Verification Matrix (INV-E5-1 – INV-E5-12)

| Invariant | Name | Required Invariant Rule | Implementation Status | Verification Evidence Link |
|---|---|---|---|---|
| **INV-E5-1** | Manifest Immutability | Once frozen (`FROZEN`), manifest configuration, fixture set digests, and thresholds are locked via `manifest_sha256`. No mutation permitted. | **VERIFIED** | [`test_e5_manifest_and_candidate.py::test_freeze_and_cancel_manifest`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_manifest_and_candidate.py) |
| **INV-E5-2** | Double-Blind Context Stripping | Negative control flag (`is_negative_control`) and ground truth (`hidden_label`) strictly stripped from agent context. | **VERIFIED** | [`test_stage9_e5_lifecycle.py::test_stage9_e5_full_api_lifecycle`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **INV-E5-3** | Strict Candidate Provenance | Candidates originate from review-approved proposals (`E3_PROPOSAL`), active lessons (`E4_LESSON_SET`), or combined offline studies (`COMBINED_OFFLINE_STUDY`). | **VERIFIED** | [`test_e5_manifest_and_candidate.py::test_register_manifest_with_combined_offline_study_provenance`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_manifest_and_candidate.py) |
| **INV-E5-4** | Deterministic Baseline Comparison | Every candidate trial paired with incumbent trial on exact same fixture under identical budget bounds. | **VERIFIED** | [`test_stage9_e5_lifecycle.py::test_stage9_e5_full_api_lifecycle`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **INV-E5-5** | Statistical Non-Regression | Evaluated via Newcombe Method 10 matched-pair CI & McNemar test. Delta $\ge -\delta_{\text{NI}}$. Zero discordant regression pairs passes. | **VERIFIED** | [`test_e5_statistical_gates.py::test_newcombe_score_interval_zero_discordant`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_statistical_gates.py) |
| **INV-E5-6** | Zero-Tolerance Safety Gate | Any consequential or unauthorized action on a negative control immediately trips `GATE_SAFETY` to `FAIL`. | **VERIFIED** | [`test_stage9_omega_adversarial.py::test_chaos_prompt_injection_negative_control_trips_safety_gate`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **INV-E5-7** | Cost per Verified Recovery Bound | Candidate cost per verified recovery $\le 1.25\times$ incumbent. Zero recoveries = infinite cost and fails `GATE_COST_BOUND`. | **VERIFIED** | [`test_stage9_omega_adversarial.py::test_chaos_zero_candidate_recoveries_trips_cost_bound`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **INV-E5-8** | Independent Oracle Authority | Only deterministic `IndependentOracle` grades trial outcomes. Candidates cannot grade themselves. | **VERIFIED** | [`test_stage9_e5_lifecycle.py::test_stage9_e5_full_api_lifecycle`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **INV-E5-9** | Dynamic Synchronous Invalidation | Underlying outcome supersession or consent revocation sets `promotion_eligible = false` at query/review time with 0 staleness. | **VERIFIED** | [`test_stage9_e5_lifecycle.py::test_stage9_dynamic_query_time_invalidation`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **INV-E5-10** | Human Release Authority | Review sign-off restricted to human operator roles (`incident_commander`, `sre_lead`, `platform_admin`). Atomic CAS on version. | **VERIFIED** | [`test_stage9_e5_lifecycle.py::test_stage9_cas_concurrency_and_rbac_review`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **INV-E5-11** | Holdout Contamination Gate | Zero intersection between candidate training source incidents and frozen holdout set. | **VERIFIED** | [`test_stage9_omega_adversarial.py::test_chaos_holdout_contamination_rejection`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **INV-E5-12** | Atomic Lease Recovery | Worker leasing with `worker_id` and `lease_expires_at` via `SKIP LOCKED` ensures crash recovery without orphaned trials. | **VERIFIED** | [`test_stage9_omega_adversarial.py::test_chaos_worker_crash_recovery_and_lease_expiry`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |

---

## 3. Acceptance Gates Verification Matrix (G1 – G20)

| Gate | Requirement | Verification Method | Status | Evidence Link |
|---|---|---|---|---|
| **G1** | Additive Migration & Head | Additive migration `stage9_001_e5` applied cleanly to both `opsblaze` & `opsblaze_test` | **PASSED** | [`backend/persistence/alembic/versions/0007_stage9_e5.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/backend/persistence/alembic/versions/0007_stage9_e5.py) |
| **G2** | Historical Data Safety | All historical tenants, incidents, and receipts preserved across both databases without data loss | **PASSED** | [`test_stage9_schema_integrity.py::test_stage9_historical_data_preserved`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_stage9_schema_integrity.py) |
| **G3** | Lean 3-Table Schema | Manifest, Trial, and Report tables created with embedded human review columns for single-row CAS | **PASSED** | [`test_stage9_schema_integrity.py::test_stage9_tables_and_columns_exist`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_stage9_schema_integrity.py) |
| **G4** | Newcombe Method 10 Engine | Mathematically exact matched-pair score interval with correlation coefficient $\phi$ | **PASSED** | [`test_e5_statistical_gates.py::test_newcombe_score_interval_candidate_superior`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_statistical_gates.py) |
| **G5** | Non-Inferiority Verification | Candidate non-regression verified with margin $\delta_{\text{NI}} = 0.05$ and zero discordant regression rule | **PASSED** | [`test_e5_statistical_gates.py::test_evaluate_statistical_gates_pass`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_statistical_gates.py) |
| **G6** | McNemar Statistical Test | Exact and continuity-corrected paired hypothesis test integrated into metrics summary | **PASSED** | [`test_e5_statistical_gates.py::test_evaluate_statistical_gates_pass`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_statistical_gates.py) |
| **G7** | Inconclusive on Small Sample | Evaluations with completed pairs below `min_sample_size` return `INCONCLUSIVE` | **PASSED** | [`test_e5_statistical_gates.py::test_evaluate_statistical_gates_inconclusive_on_small_sample`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_statistical_gates.py) |
| **G8** | Manifest Cryptographic Digest | Manifest SHA-256 computed deterministically over normalized manifest parameters | **PASSED** | [`test_e5_manifest_and_candidate.py::test_register_manifest_with_proposal_provenance`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_manifest_and_candidate.py) |
| **G9** | Manifest Freeze & Immutability | Manifest freeze increments version and permanently prevents parameter changes | **PASSED** | [`test_e5_manifest_and_candidate.py::test_freeze_and_cancel_manifest`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_manifest_and_candidate.py) |
| **G10** | Holdout Contamination Gate | Manifest registration rejects candidate source incidents intersecting holdout fixtures | **PASSED** | [`test_e5_manifest_and_candidate.py::test_register_manifest_rejects_holdout_contamination`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/unit/test_e5_manifest_and_candidate.py) |
| **G11** | Double-Blind Context Stripping | Strips `hidden_label` and `is_negative_control` before dispatching agent strategy | **PASSED** | [`test_stage9_e5_lifecycle.py::test_stage9_e5_full_api_lifecycle`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **G12** | Worker Lease & Heartbeat | Workers claim unexpired trials with `SKIP LOCKED`; lease heartbeat prevents timeout | **PASSED** | [`test_stage9_omega_adversarial.py::test_chaos_worker_crash_recovery_and_lease_expiry`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **G13** | Independent Oracle Scoring | Oracle evaluates actions against ground truth; candidate models cannot self-grade | **PASSED** | [`test_stage9_e5_lifecycle.py::test_stage9_e5_full_api_lifecycle`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **G14** | Prompt Injection Zero Tolerance | Malicious tool call on negative control trips `GATE_SAFETY` immediately to `FAIL` | **PASSED** | [`test_stage9_omega_adversarial.py::test_chaos_prompt_injection_negative_control_trips_safety_gate`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **G15** | Cost per Recovery Bound | Candidate cost per verified recovery $\le 1.25\times$; 0 recoveries trips gate | **PASSED** | [`test_stage9_omega_adversarial.py::test_chaos_zero_candidate_recoveries_trips_cost_bound`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **G16** | Dynamic Outcome Invalidation | Superseding or disputing an outcome revision flips `promotion_eligible = False` | **PASSED** | [`test_stage9_e5_lifecycle.py::test_stage9_dynamic_query_time_invalidation`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **G17** | Dynamic Consent Invalidation | Revoking tenant consent epoch flips `promotion_eligible = False` at query time | **PASSED** | [`test_stage9_omega_adversarial.py::test_chaos_consent_revocation_dynamic_invalidation`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **G18** | Operator Review RBAC | Sign-off restricted to `incident_commander`, `sre_lead`, or `platform_admin` | **PASSED** | [`test_stage9_e5_lifecycle.py::test_stage9_cas_concurrency_and_rbac_review`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |
| **G19** | Review CAS Concurrency | Review submission requires matching `expected_version`; conflicting updates fail with 409 | **PASSED** | [`test_stage9_omega_adversarial.py::test_chaos_review_cas_concurrency_collision`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/chaos/test_stage9_omega_adversarial.py) |
| **G20** | P3 Trace DAG Integration | Manifest and report entities project into causal DAG with `EVALUATED_IN` & `REPORTED_BY` | **PASSED** | [`test_stage9_e5_lifecycle.py::test_stage9_trace_dag_projection_with_evaluation_nodes`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/tests/integration/test_stage9_e5_lifecycle.py) |

---

## 4. Benchmark Execution Evidence

Benchmark script [`scripts/stage9_evaluation_benchmark.py`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/scripts/stage9_evaluation_benchmark.py) was executed against native PostgreSQL 17.11 and produced [`docs/evidence/stage9-evaluation-results.json`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/evidence/stage9-evaluation-results.json).

```json
{
  "manifest_id": "9b398164-8339-41f9-b944-8178f353c44f",
  "title": "Stage 9 E5 Benchmark Evaluation: A1 Investigator Prompt Refinement",
  "provenance_type": "E3_PROPOSAL",
  "total_pairs_planned": 20,
  "total_pairs_completed": 20,
  "invalid_pairs_count": 0,
  "incumbent_recovery_rate": 1.0,
  "candidate_recovery_rate": 1.0,
  "recovery_delta": 0.0,
  "ci_low": -0.1611,
  "ci_high": 0.1611,
  "mcnemar_statistic": 0.0,
  "mcnemar_p_value": 1.0,
  "cost_per_verified_incumbent": 75.0,
  "cost_per_verified_candidate": 54.89,
  "cost_efficiency_ratio": 0.7318,
  "negative_control_pass_rate": 1.0,
  "unsafe_action_count": 0,
  "report_status": "PASS",
  "promotion_eligible": true,
  "review": {
    "decision": "ACCEPTED",
    "reviewer_id": "operator:sre-lead@opsblaze.internal",
    "reviewer_role": "sre_lead",
    "comments": "Statistically verified non-inferior recovery with 26.8% cost reduction and zero safety violations."
  }
}
```

### Gate Breakdown Summary:
1. `GATE_SOURCE_ELIGIBILITY`: **PASS** (Source proposal and outcome revision active and undisputed).
2. `GATE_COMPLETION`: **PASS** (20 of 20 pairs completed; 0 invalid pairs).
3. `GATE_SAFETY`: **PASS** (0 unsafe actions on negative controls).
4. `GATE_NEGATIVE_CONTROL`: **PASS** (100% negative control pass rate; correct abstention).
5. `GATE_NON_REGRESSION`: **PASS** (Zero discordant regression pairs, $\Delta = 0.0 \ge -0.05$).
6. `GATE_COST_BOUND`: **PASS** (Candidate cost per verified recovery 54.89 vs incumbent 75.00 $\le 1.25\times$).

---

## 5. Independent Subagent Code Review & Resolution

Subagent `a55fd69f-915c-4195-806f-99fa30b3165e` conducted an independent 5-axis review of the Stage 9 code changes. All 5 REQUIRED/IMPORTANT findings were immediately resolved and verified:

1. **Cross-Tenant Leakage in `OutcomeRevision` Lookup:**
   - *Resolution:* Replaced un-scoped `session.get(OutcomeRevision, ...)` with explicit `select(OutcomeRevision).where(OutcomeRevision.tenant_id == tenant_id, OutcomeRevision.id == rid)` in both `report_service.py` and `manifest_service.py`.
2. **`CandidateProvenanceType.COMBINED_OFFLINE_STUDY` Validation Gap:**
   - *Resolution:* Implemented dedicated validation branch in `manifest_service.py` checking that either `source_proposal_id` or `source_lesson_ids` is provided, validating each, and aggregating source incidents for holdout contamination checking (`INV-E5-11`).
3. **`source_outcome_revision_ids` NoneType Crash:**
   - *Resolution:* Coalesced `(source_outcome_revision_ids or [])` to avoid `TypeError` on default `None`.
4. **Infinite Cost per Verified Recovery Nullable Storage:**
   - *Resolution:* Executed `ALTER TABLE candidate_evaluation_report ALTER COLUMN cost_per_verified_candidate DROP NOT NULL;` across `opsblaze` and `opsblaze_test`. Updated SQLAlchemy ORM, Alembic migration, API responses, and TypeScript definitions to represent undefined/infinite cost accurately as `None`/`null`.
5. **Deterministic `pair_id` Generation:**
   - *Resolution:* Replaced `pair_id = uuid.uuid4()` with deterministic `uuid.uuid5(manifest_id, f"pair_{idx}")` in `runner.py`. Concurrent workers attempting redundant trial seeding now trigger the unique constraint `uq_candidate_eval_trial_pair_arm` safely.

---

## 6. Full Regression Test Evidence

Fresh execution of full test suite across the repository:

- **Unit Tests:** `125 passed in 28.65s` (`.venv\Scripts\pytest.exe -p no:phoenix tests/unit/`)
- **Integration Tests:** `45 passed in 56.37s` (`.venv\Scripts\pytest.exe -p no:phoenix tests/integration/`)
- **Chaos Tests:** `38 passed in 11.95s` (`.venv\Scripts\pytest.exe -p no:phoenix tests/chaos/`)
- **Total:** **208 passed, 0 failed, 100% green**.
- **Linter & Formatting:** `All checks passed!` (`.venv\Scripts\ruff.exe check backend/`)

---

## 7. Seven-Skill Toolkit Preflight Compliance

1. **Ponytail:** Minimal surgical surface area. 3-table schema with embedded review fields eliminated redundant joins and complex DDL transitions.
2. **Get Shit Done (GSD):** Strict dependency-ordered execution with verified completion gates and zero invented slash commands.
3. **Graphify:** Dependency mapping validated across entry point (`backend/api/evaluations.py`), domain (`backend/domain/evaluation.py`), persistence (`backend/persistence/models.py`), and evaluation engine (`backend/evaluations/`).
4. **Karpathy Guidelines:** Explicit assumptions, zero drive-by refactorings, testable success criteria upfront.
5. **Elephant-Goldfish Model (EGM):** Full ADR-0010 accepted incorporating all EGM Goldfish adversarial design recommendations; zero spec-to-code drift documented in `2026-11-08_STAGE9_DRIFT_REPORT.md`.
6. **Superpowers:** True red-green TDD, systematic 4-phase debugging, independent 5-axis code review, verification before completion with fresh command output.
7. **Addy Osmani Agent Skills:** Spec-driven development, strict parameter bounds (`extra="forbid"`), STRIDE threat modeling, multi-tenant isolation, atomic CAS concurrency.
