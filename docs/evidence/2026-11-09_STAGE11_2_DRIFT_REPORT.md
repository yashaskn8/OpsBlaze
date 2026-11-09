# OPSBLAZE TITAN-20 — STAGE 11.2
## ARCHITECTURE & SPEC-TO-CODE DRIFT AUDIT REPORT (EGM)

* **Date:** 2026-11-09
* **Milestone:** Stage 11.2 — Production Identity, Durable Guardrails & Isolated Shadow Execution
* **Audited Against:** ADR-0013, Stage 11.2 Mandate, and `AGENTS.md` Invariants
* **Auditor:** Elephant-Goldfish Model (EGM) Architecture Verification Framework
* **Overall Status:** **ZERO ADVERSE DRIFT (100% SPEC CONFORMANCE)**

---

## 1. Scope and Objective

This audit examines the actual source code, database migrations, security controls, and operational workflows implemented during Stage 11.2 against the approved architectural decisions in **ADR-0013** and the non-negotiable trust boundaries in **AGENTS.md**.

The audit evaluates four critical dimensions:
1. **Identity & Authority Boundary:** Server-side role resolution vs. client assertions.
2. **Outbox Partitioning & Concurrency:** Multi-tenant outbox table separation, worker leases, and deduplication.
3. **Shadow Isolation & Cryptographic Integrity:** Absence of consequential authority and verification of dual RFC 8785 + HMAC-SHA256 bundles.
4. **Failure Containment & Graceful Lifecycle:** Crash resilience and graceful shutdown in multi-worker environments.

---

## 2. Invariant Drift Analysis

### 2.1 Identity & Authority (Gate A & Gate E)
* **Spec Invariant:** No client, model, or proxy may mint or assert roles. Roles must be resolved server-side from `IdentityMembership` bound to immutable `(idp_issuer, idp_subject, tenant_id)`.
* **Implementation Audit:**
  - `backend/api/auth.py`: `TokenExchangeRequest` contains only `idp_token` and `tenant_id`. Client role parameters are prohibited (`extra="forbid"`).
  - `backend/domain/identity_provider.py`: Introspects tokens against IdP using RFC 7662 with SSRF defenses (blocking loopback `127.0.0.0/8`, metadata endpoints `169.254.169.254`, `metadata.google.internal`, and non-HTTPS URLs).
  - `backend/api/auth_deps.py`: Resolves membership synchronously from PostgreSQL `identity_memberships` and checks `membership.is_active` on every authenticated request, guaranteeing 0-second revocation.
* **Verdict:** **CONFORMANT — Zero Drift.**

### 2.2 Outbox Partitioning & Dispatcher Isolation (Gate B)
* **Spec Invariant:** B1 incident dispatching must not be contaminated or stalled by release guardrail or shadow evaluation events. Outbox events must use `FOR UPDATE SKIP LOCKED` partitioned by `kind`.
* **Implementation Audit:**
  - `backend/persistence/models.py`: Outbox model extended with `kind: String(64) DEFAULT 'INCIDENT_START'`, `dedupe_key: String(128)`, `payload: JSON`, `last_error: String(2048)`, `lease_owner: String(128)`, `incident_id: Uuid nullable=True`.
  - `backend/persistence/repositories.py`:
    - `OutboxRepository.claim_batch` explicitly queries `where(Outbox.kind == OUTBOX_KIND_INCIDENT_START)`.
    - `OutboxRepository.claim_guardrail_batch` queries `where(Outbox.kind == OUTBOX_KIND_RELEASE_GUARDRAIL)`.
    - `OutboxRepository.claim_shadow_batch` queries `where(Outbox.kind == OUTBOX_KIND_RELEASE_SHADOW)`.
  - All claiming queries utilize `with_for_update(skip_locked=True)`.
  - Worker lease timeouts are enforced (15s for guardrails, 30s for shadow).
* **Verdict:** **CONFORMANT — Zero Drift.**

### 2.3 Shadow Execution Isolation (Gate C)
* **Spec Invariant:** Candidate policy in shadow mode must run side-by-side with incumbent against identical verified evidence, but must have ZERO consequential authority (no B2 fence, no B3 receipt, no P2 approval, no tool execution).
* **Implementation Audit:**
  - `backend/releases/shadow_executor.py`:
    - Validates both candidate and incumbent policy bundles using RFC 8785 (JCS) canonicalization and HMAC-SHA256 signatures before running.
    - Candidate evaluation operates strictly on the immutable `evidence_manifest`.
    - No resource reservation calls (`B2`) or external effect receipt creations (`B3`) are present.
    - Mid-flight kill switch checking: re-verifies release state prior to recording observations; if emergency rollback occurred mid-execution, candidate findings are discarded and no observation is committed.
* **Verdict:** **CONFORMANT — Zero Drift.**

### 2.4 Multi-Worker Lifecycle & Teardown (Gate D & Gate G)
* **Spec Invariant:** In-process dispatchers must start on application boot, poll with backoff, and shut down cleanly within 5 seconds without dropping in-flight database transactions.
* **Implementation Audit:**
  - `backend/api/main.py`: `lifespan` context manager instantiates `GuardrailOutboxDispatcher` and `ShadowOutboxDispatcher` when `enable_in_process_dispatchers=True`.
  - `shutdown()` method sets running flags to `False` and awaits task cancellation with `asyncio.wait_for(timeout=5.0)`.
  - Foreign key cascading and model teardown in `tests/conftest.py` ordered correctly (`Outbox` / `Incident` before `PolicyRelease` / `PolicyBundle`), preventing foreign key violation deadlocks.
* **Verdict:** **CONFORMANT — Zero Drift.**

---

## 3. Summary of Code vs. Spec Alignments

| Domain Area | ADR-0013 Requirement | Actual Code Implementation | Drift Status |
|---|---|---|---|
| RFC 7662 Introspection | HTTP POST to IdP `/introspect` | `TokenIntrospectionClient.introspect_token()` with SSRF protection | Exact Match |
| Zero-Second Revocation | Instant deactivation via DB | `get_authenticated_user_context` checks `membership.is_active` on every request | Exact Match |
| Outbox Kind Segregation | Distinct event types | `OUTBOX_KIND_INCIDENT_START`, `RELEASE_GUARDRAIL`, `RELEASE_SHADOW_EVALUATION` | Exact Match |
| Lock-Free Claiming | `FOR UPDATE SKIP LOCKED` | `with_for_update(skip_locked=True)` in all three batch claimers | Exact Match |
| Dual Bundle Signing | RFC 8785 canonical hash + HMAC | `PolicyBundleService.verify_bundle_integrity()` invoked on candidate & incumbent | Exact Match |
| Shadow Containment | Zero consequential side-effects | Candidate runs in read-only observation path; telemetry saved to `release_event_audit` | Exact Match |
| Kill-Switch Abort | Mid-flight candidate abort | CAS checks in `ShadowExecutor.execute_shadow_evaluation()` discard on rollback | Exact Match |

---

## 4. Conclusion & Sign-Off

The Elephant-Goldfish Model (EGM) drift audit confirms that **zero adverse architectural drift** exists in the Stage 11.2 codebase. All specifications in ADR-0013 have been implemented accurately and exhaustively.
