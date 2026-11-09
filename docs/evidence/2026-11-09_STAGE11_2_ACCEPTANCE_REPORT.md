# OPSBLAZE TITAN-20 — STAGE 11.2
## PRODUCTION IDENTITY, DURABLE GUARDRAILS & ISOLATED SHADOW EXECUTION ACCEPTANCE REPORT

* **Date:** 2026-11-09
* **Repository:** `https://github.com/yashaskn8/OpsBlaze`
* **Local Root:** `C:\Users\prana\OneDrive\Desktop\OpsBlaze`
* **Target Branch:** `main`
* **Baseline Git Commit:** `3df54ba` (Stage 11.1 published baseline)
* **Architecture:** Python 3.14 + FastAPI + PostgreSQL 17 + Temporal + React 19 + TypeScript
* **Evaluation Status:** **ACCEPTED & FULLY VERIFIED (9 / 9 GATES PASSED)**

---

## 1. Executive Summary

Stage 11.2 completes the transition of OpsBlaze / TITAN-20 from prototype authorization, in-memory guardrail checks, and synchronous routing stubs into an enterprise-grade, distributed, fault-tolerant control plane.

Prior to Stage 11.2:
1. **Identity:** Identity relied on local username/password authentication with pre-seeded test principals, leaving production vulnerable to credential reuse and lacking RFC 7662 token introspection against corporate IdPs (Okta, Keycloak, Entra ID).
2. **Guardrails:** Runtime guardrail evaluation was performed synchronously inside the database transaction of incident state transitions. A failure in statistical rate metrics computation could fail incident progression, and in-flight guardrail evaluations were lost on process crash.
3. **Shadow Evaluation:** Incident routing in `SHADOW` state was a synchronous stub without cryptographic bundle verification, without dual candidate/incumbent execution against identical verified evidence, and without complete isolation from consequential effects.
4. **Outbox Contention:** The B1 incident outbox dispatcher claimed any pending outbox row without partitioning by event kind, causing non-incident events to corrupt workflow dispatching.

In Stage 11.2, all three subsystems have been re-engineered with PostgreSQL 17 durability, strict cryptographic verification, zero-trust server-side role resolution, and full multi-worker process decoupling.

---

## 2. Gate-by-Gate Verification Matrix

| Gate | Focus Area | Core Mitigations | Test Evidence | Verdict |
|---|---|---|---|---|
| **Gate A** | **Production Identity Exchange** | RFC 7662 token introspection (`TokenIntrospectionClient`) with SSRF protection (loopback/link-local/cloud metadata IP blocking, HTTPS enforcement, redirects disabled); server-authoritative `IdentityMembership` table mapping `(idp_issuer, idp_subject, tenant_id)` to immutable `roles`; synchronous check of `is_active` on every authenticated request ensuring 0-second revocation guarantee; rejection of client-asserted roles; deprecation of password login in production (`auth_allow_password_login=False`). | `tests/security/test_stage11_2_identity_exchange.py` (12/12 passed) | **PASSED** |
| **Gate B** | **Durable Guardrail Execution** | Atomic insertion of `Outbox(kind="RELEASE_GUARDRAIL", dedupe_key=f"guardrail:{release_id}")` within the incident state transition transaction; partition-aware claiming using `SELECT ... FOR UPDATE SKIP LOCKED WHERE kind = 'RELEASE_GUARDRAIL'`; 15s worker lease ownership; deduplication preventing redundant evaluation; exponential backoff with dead-lettering (`max_retries=5`); explicit isolation of B1 incident dispatcher (`kind == 'INCIDENT_START'`). | `tests/unit/test_stage11_2_guardrail_dispatcher.py` (6/6 passed) | **PASSED** |
| **Gate C** | **Isolated Shadow Execution** | Outbox event enqueue `RELEASE_SHADOW_EVALUATION` during incident routing in `SHADOW` state; dual RFC 8785 (JCS) + HMAC-SHA256 integrity verification of both candidate and incumbent policy bundles prior to execution; candidate evaluated solely against identical immutable evidence snapshot; complete isolation from consequential authority (no B2 fence, no B3 receipt, no P2 approval card, no A2 mutation grant); mid-flight kill-switch invalidation discarding candidate findings. | `tests/unit/test_stage11_2_shadow_execution.py` (6/6 passed) | **PASSED** |
| **Gate D** | **Runtime & Worker Lifecycle** | Background workers (`GuardrailOutboxDispatcher`, `ShadowOutboxDispatcher`) wired to FastAPI application lifespan; configurable polling intervals (default 2.0s); graceful shutdown with 5.0s task drain; error containment preventing unhandled exceptions from crashing dispatchers; support for external multi-worker process separation (`enable_in_process_dispatchers=False`). | Verified via FastAPI lifespan in `backend/api/main.py` and dispatcher unit tests | **PASSED** |
| **Gate E** | **Security & Authority Isolation** | STRIDE threat modeling addressed: Spoofing blocked via cryptographic introspection + HMAC bundle signing; Tampering blocked via RFC 8785 canonicalization + CAS version fencing; Repudiation blocked via immutable outbox and `release_event_audit` logs; Information Disclosure blocked via secret masking in logs and sanitization; Denial of Service blocked via `SKIP LOCKED` lock-free polling and exponential backoff; Elevation of Privilege blocked via server-derived roles and 0 consequential authority in shadow. | `tests/security/test_stage11_2_identity_exchange.py`, `tests/security/test_stage11_1_release_assurance_adversarial.py` | **PASSED** |
| **Gate F** | **Observability & Readiness** | Outbox claiming metrics (`claimed_count`, `delivered_count`, `retry_count`, `dead_letter_count`); shadow execution latency and agreement tracking (`shadow_agreement_rate`); structured runbooks authored: `identity-membership-provisioning.md`, `durable-guardrail-operations.md`, `shadow-evaluation-operations.md`. | Verified via runbooks and dispatcher logging instrumentation | **PASSED** |
| **Gate G** | **Database Integrity & Migrations** | Alembic migration `0009_stage11_2_outbox_identity.py` successfully applied to both `opsblaze` (development) and `opsblaze_test` (test) on PostgreSQL 17; created `identity_memberships` table with unique constraint `uq_idp_subject_tenant`; altered `outbox` table adding `kind`, `dedupe_key`, `payload`, `last_error`, `lease_owner`, `incident_id` (nullable), partial unique index `uq_outbox_dedupe_in_flight`, and composite index `ix_outbox_kind_pending`. | Alembic revision verified at `stage11_2_001_outbox_identity (head)` | **PASSED** |
| **Gate H** | **Deterministic Regression & CI** | All 61 automated tests across Unit, Security, and Integration suites execute deterministically and pass 100%; E5 evaluation suite (8 tests) passes 100%; zero HTML application code; Ruff linter clean (0 errors); Mypy typechecker clean (0 errors across 101 files); Frontend TSX clean (0 type errors, 0 build errors); `.github/workflows/ci.yml` updated with Alembic migration and Stage 11.2 suites. | 61/61 tests passed in 16.60s; 8/8 E5 tests passed in 5.33s | **PASSED** |
| **Gate I** | **Architecture Documentation** | Architecture Decision Record ADR-0013 published detailing all design decisions, trade-offs, and invariants; three operational runbooks authored; acceptance report published. | `docs/adr/0013-stage11.2-production-identity-durable-guardrails-shadow-execution.md` | **PASSED** |

---

## 3. Database Schema & Migration Evidence

### 3.1 Migration Revision: `0009_stage11_2_outbox_identity.py`
* **Revision ID:** `stage11_2_001_outbox_identity`
* **Down Revision:** `stage11_001_e6_release`

```sql
-- Identity Memberships Table
CREATE TABLE identity_memberships (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    idp_issuer VARCHAR(255) NOT NULL,
    idp_subject VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    display_name VARCHAR(255),
    roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_idp_subject_tenant UNIQUE (idp_issuer, idp_subject, tenant_id)
);
CREATE INDEX ix_identity_memberships_lookup ON identity_memberships (idp_issuer, idp_subject, tenant_id, is_active);

-- Outbox Model Expansion
ALTER TABLE outbox ADD COLUMN kind VARCHAR(64) NOT NULL DEFAULT 'INCIDENT_START';
ALTER TABLE outbox ADD COLUMN dedupe_key VARCHAR(128);
ALTER TABLE outbox ADD COLUMN payload JSONB;
ALTER TABLE outbox ADD COLUMN last_error VARCHAR(2048);
ALTER TABLE outbox ADD COLUMN lease_owner VARCHAR(128);
ALTER TABLE outbox ALTER COLUMN incident_id DROP NOT NULL;

CREATE UNIQUE INDEX uq_outbox_dedupe_in_flight ON outbox (dedupe_key) 
    WHERE delivered = FALSE AND dedupe_key IS NOT NULL;
CREATE INDEX ix_outbox_kind_pending ON outbox (kind, delivered, next_attempt_at);
```

### 3.2 Migration Verification
* **Database `opsblaze`:** Current revision = `stage11_2_001_outbox_identity (head)`
* **Database `opsblaze_test`:** Current revision = `stage11_2_001_outbox_identity (head)`

---

## 4. Automated Test Execution Evidence

### 4.1 Stage 11 & Stage 11.2 Combined Regression Suite
**Command:**
```powershell
.venv\Scripts\python -m pytest `
  tests/unit/test_no_unauthorized_html.py `
  tests/unit/test_e6_bundle_integrity.py `
  tests/unit/test_e6_canary_assignment.py `
  tests/unit/test_e6_guardrails.py `
  tests/unit/test_e6_kill_switch_and_fencing.py `
  tests/unit/test_e6_release_lifecycle.py `
  tests/security/test_stage11_1_auth_hardening.py `
  tests/security/test_stage11_1_release_assurance_adversarial.py `
  tests/security/test_stage11_2_identity_exchange.py `
  tests/unit/test_stage11_2_guardrail_dispatcher.py `
  tests/unit/test_stage11_2_shadow_execution.py `
  tests/integration/test_stage11_e6_lifecycle.py -v
```

**Results:**
```text
============================= test session starts =============================
platform win32 -- Python 3.14.7, pytest-9.2.0, pluggy-1.7.0
rootdir: C:\Users\prana\OneDrive\Desktop\OpsBlaze
configfile: pyproject.toml
plugins: anyio-4.16.0, asyncio-1.4.1.post1
asyncio: mode=Mode.AUTO, debug=False

collected 61 items

tests/unit/test_no_unauthorized_html.py::test_tracked_files_no_unauthorized_html PASSED [  1%]
tests/unit/test_no_unauthorized_html.py::test_no_html_in_backend_docs_or_scripts PASSED [  3%]
tests/unit/test_no_unauthorized_html.py::test_vite_bootstrap_is_minimal PASSED [  4%]
tests/unit/test_no_unauthorized_html.py::test_backend_no_server_side_html_rendering PASSED [  6%]
tests/unit/test_no_unauthorized_html.py::test_react_tsx_components_exist PASSED [  8%]
tests/unit/test_e6_bundle_integrity.py::test_rfc8785_canonicalization_determinism PASSED [  9%]
tests/unit/test_e6_bundle_integrity.py::test_bundle_digest_and_signature_verification PASSED [ 11%]
tests/unit/test_e6_bundle_integrity.py::test_bundle_tampering_detection PASSED [ 13%]
tests/unit/test_e6_bundle_integrity.py::test_bundle_service_seal_and_verify PASSED [ 14%]
tests/unit/test_e6_canary_assignment.py::test_consistent_hashing_repeatability PASSED [ 16%]
tests/unit/test_e6_canary_assignment.py::test_shadow_mode_routes_to_incumbent PASSED [ 18%]
tests/unit/test_e6_canary_assignment.py::test_active_mode_routes_to_candidate PASSED [ 19%]
tests/unit/test_e6_canary_assignment.py::test_kill_switch_forces_incumbent_routing PASSED [ 21%]
tests/unit/test_e6_canary_assignment.py::test_canary_distribution_spread PASSED [ 22%]
tests/unit/test_e6_guardrails.py::test_class_1_zero_tolerance_invariant_breach_triggers_immediate_rollback PASSED [ 24%]
tests/unit/test_e6_guardrails.py::test_class_2_statistical_rate_breach_triggers_rollback PASSED [ 26%]
tests/unit/test_e6_kill_switch_and_fencing.py::test_kill_switch_instantly_isolates_and_fences PASSED [ 27%]
tests/unit/test_e6_release_lifecycle.py::test_full_release_lifecycle_happy_path PASSED [ 29%]
tests/unit/test_e6_release_lifecycle.py::test_separation_of_duties_enforced PASSED [ 31%]
tests/unit/test_e6_release_lifecycle.py::test_cas_version_conflict_rejected PASSED [ 32%]
tests/unit/test_e6_release_lifecycle.py::test_anti_split_brain_single_active_release PASSED [ 34%]
tests/security/test_stage11_1_auth_hardening.py::test_missing_production_secrets_prevent_startup PASSED [ 36%]
tests/security/test_stage11_1_auth_hardening.py::test_compromised_default_secret_rejected_in_production PASSED [ 37%]
tests/security/test_stage11_1_auth_hardening.py::test_token_algorithm_strictness PASSED [ 39%]
tests/security/test_stage11_1_auth_hardening.py::test_token_revocation PASSED [ 40%]
tests/security/test_stage11_1_auth_hardening.py::test_model_cannot_claim_human_authority PASSED [ 42%]
tests/security/test_stage11_1_auth_hardening.py::test_public_token_endpoint_rejects_arbitrary_client_roles PASSED [ 44%]
tests/security/test_stage11_1_auth_hardening.py::test_authenticated_user_login_uses_server_derived_roles PASSED [ 45%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_tenant_header_alone_cannot_authenticate PASSED [ 47%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_p2_approval_derives_reviewer_from_principal PASSED [ 49%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_bot_cannot_approve_p2_plan PASSED [ 50%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_unauthorized_role_cannot_promote_release PASSED [ 52%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_unauthorized_role_cannot_modify_canary_percentage PASSED [ 54%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_public_guardrail_endpoint_rejects_simulated_breaches PASSED [ 55%]
tests/security/test_stage11_1_release_assurance_adversarial.py::test_rolled_back_release_blocks_candidate_consequential_mutation PASSED [ 57%]
tests/security/test_stage11_2_identity_exchange.py::test_ssrf_validation_blocks_malicious_urls PASSED [ 59%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_success PASSED [ 60%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_inactive_rejected PASSED [ 62%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_issuer_mismatch_rejected PASSED [ 63%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_audience_mismatch_rejected PASSED [ 65%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_expired_rejected PASSED [ 67%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_missing_membership_rejected PASSED [ 68%]
tests/security/test_stage11_2_identity_exchange.py::test_token_exchange_deactivated_membership_rejected PASSED [ 70%]
tests/security/test_stage11_2_identity_exchange.py::test_cross_tenant_membership_isolation PASSED [ 72%]
tests/security/test_stage11_2_identity_exchange.py::test_client_cannot_assert_roles_in_exchange PASSED [ 73%]
tests/security/test_stage11_2_identity_exchange.py::test_zero_second_membership_revocation_guarantee PASSED [ 75%]
tests/security/test_stage11_2_identity_exchange.py::test_production_rejects_password_login PASSED [ 77%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_terminal_incident_enqueues_guardrail_outbox PASSED [ 78%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_b1_dispatcher_ignores_guardrail_outbox PASSED [ 80%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_guardrail_dispatcher_claims_and_delivers PASSED [ 81%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_guardrail_dispatcher_triggers_rollback_on_invariant_breach PASSED [ 83%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_guardrail_dispatcher_retry_on_transient_failure PASSED [ 85%]
tests/unit/test_stage11_2_guardrail_dispatcher.py::test_lease_expiry_allows_reclamation PASSED [ 86%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_routing_enqueues_outbox_event PASSED [ 88%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_executor_runs_and_records_observation PASSED [ 90%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_executor_integrity_failure_trips_invariant_breach PASSED [ 91%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_executor_aborts_on_kill_switch_or_state_mismatch PASSED [ 93%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_executor_mid_flight_kill_switch_discards_candidate_findings PASSED [ 95%]
tests/unit/test_stage11_2_shadow_execution.py::test_shadow_dispatcher_claims_and_delivers PASSED [ 96%]
tests/integration/test_stage11_e6_lifecycle.py::test_stage11_full_api_release_lifecycle PASSED [ 98%]
tests/integration/test_stage11_e6_lifecycle.py::test_stage11_emergency_kill_switch_api PASSED [100%]

============================= 61 passed in 16.60s =============================
```

### 4.2 Stage 10 (E5) Auth and Contract Closure Suite
**Command:**
```powershell
.venv\Scripts\python -m pytest tests/unit/test_e5_auth_and_contract_closure.py -v
```

**Results:**
```text
collected 8 items

tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_bodyless_or_malformed_requests PASSED [ 12%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_version_conflicts_rejected PASSED [ 25%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_run_requests_enforce_contract PASSED [ 37%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_client_supplied_reviewer_role_cannot_grant_authority PASSED [ 50%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_unauthenticated_users_cannot_approve PASSED [ 62%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_cross_tenant_evaluations_inaccessible PASSED [ 75%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_source_invalidation_blocks_review PASSED [ 87%]
tests/unit/test_e5_auth_and_contract_closure.py::test_gate0_successful_evaluation_and_review_workflow PASSED [100%]

============================== 8 passed in 5.33s ==============================
```

---

## 5. Static Quality & Build Evidence

| Tool / Check | Command | Result |
|---|---|---|
| **Ruff Linter** | `.venv\Scripts\python -m ruff check backend tests` | **All checks passed! (0 errors)** |
| **Mypy Typechecker** | `.venv\Scripts\python -m mypy backend` | **Success: no issues found in 101 source files** |
| **Frontend Lint** | `npm --prefix frontend run lint` | **0 errors (15 warnings, non-blocking)** |
| **Frontend Typecheck** | `npm --prefix frontend run typecheck` | **0 errors (`tsc --noEmit` clean)** |
| **Frontend Build** | `npm --prefix frontend run build` | **Built in 286ms (`dist/index.html` 0.45 kB, JS 327.71 kB)** |

---

## 6. Architecture Drift Verification

An EGM spec-to-code audit was performed comparing the implementation against ADR-0013 and `AGENTS.md`:
* **Zero Client-Asserted Roles:** Validated that neither `/v1/auth/exchange` nor `/v1/auth/login` accepts role assertions. All roles are fetched directly from PostgreSQL `identity_memberships`.
* **Zero Consequential Authority in Shadow Mode:** The candidate policy executor receives zero mutation permissions, cannot mint fences (`B2`), cannot issue receipts (`B3`), cannot request approvals (`P2`), and cannot execute tools outside observation mode.
* **Dual Cryptographic Verification:** Both incumbent and candidate bundles are verified with RFC 8785 canonical JSON formatting and HMAC-SHA256 signatures before execution. Tampered bundles immediately fail closed and record an invariant breach.
* **Worker Lease & Dead-Letter Safety:** Outbox worker claims use `FOR UPDATE SKIP LOCKED` with explicit 15s/30s lease expirations and exponential backoff, preventing stuck outbox items or split-brain multi-worker processing.

---

## 7. Operational Readiness & Runbooks

Three production runbooks have been authored and verified:
1. `docs/runbooks/identity-membership-provisioning.md`: Procedures for configuring enterprise IdPs, provisioning user memberships, role mappings, and instant 0-second access revocation.
2. `docs/runbooks/durable-guardrail-operations.md`: Architecture of outbox-backed guardrails, retry monitoring, dead-letter remediation, and automated emergency rollback procedures.
3. `docs/runbooks/shadow-evaluation-operations.md`: Lifecycle of shadow outbox events, dual bundle execution, candidate telemetry analysis, and candidate-to-canary promotion criteria.

---

## 8. Final Stage 11.2 Acceptance Verdict

**VERDICT: ACCEPTED & FULLY VERIFIED (100% PASS)**

The Stage 11.2 implementation strictly complies with all architectural constraints, security boundaries, and operational invariants specified in `AGENTS.md` and ADR-0013. The codebase is clean, statically validated, fully migrated, and ready for publication upon user authorization.
