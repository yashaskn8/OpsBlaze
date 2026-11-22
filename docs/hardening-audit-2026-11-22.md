# OpsBlaze cross-system hardening audit — 2026-11-22

> Simulator-only engineering audit. This is **not** an independent penetration
> test, external adapter certificate, privacy erasure certificate, E5
> evaluation, or production release authorization.

## Audit baseline and verified changes

- Baseline: `802fe1fa291115b515734f48219d26a7e26a5562` on `main`.
- Verified hardening tip: `5274feeb7c1a16ed207d86dbd52869b0f551648a`.
- Native GitHub Actions run: https://github.com/yashaskn8/OpsBlaze/actions/runs/45005143248 — 10/10 jobs green.
- Execution: direct `main` commits, no Docker or external effect activation.
- Scope: critical-path code review and regression testing of authentication,
  admission, outbox/Temporal transport, simulator effects, projector privacy,
  lessons/memory, PostgreSQL restore and CI configuration. Not a line-by-line
  certified review of every code path.

### Correctness/security defects addressed

| Risk | Change | Regression evidence |
|---|---|---|
| B4 false aged-active alerts after cancellation/review acknowledgment | Reset `Incident.phase_entered_at` with manual state transitions | `test_status_changes_reset_active_phase_age` |
| P1/B4 slow or out-of-order worker heartbeats overwrite fresher presence | Monotonic conflict update for each heartbeat role | `test_delayed_heartbeat_cannot_overwrite_newer_presence` |
| P3 secrets from exception strings reaching worker logs | Dispatcher and heartbeat log only safe class/role/intent details | `test_delivery_errors_never_log_provider_or_payload_secrets` |
| E1/E4 poisoned, cross-incident or malformed stored evidence identifiers being returned as valid memory | Bounded, tenant/incident/target/digest validation at capsule read and lesson-source checks; batch proof reads | `test_redteam_capsule_evidence_integrity_blocks_poisoned_memory` |
| B1 dispatcher dies on claim or acknowledgment persistence outage | Fail-safe retry through existing outbox, without invented ACK or reissue authority; redact DB exception details | `test_dispatch_claim_store_outage_is_retryable_and_redacted`, `test_dispatch_ack_outage_keeps_intent_for_stable_retry` |

The 10-job run also includes migration/schema checks, native PostgreSQL RLS
and dump/restore sentinels, Temporal integration/history replay, API/unit
regressions, security negative tests, CodeQL, strict dependency audits and
frontend compilation. These are **lab** verification boundaries.

## Feature-area review and remaining obligations

| Feature cluster | Simulator baseline | Unclosed acceptance proof |
|---|---|---|
| B1–B4 — Workflow, reservation, effects, budgets | Implemented and failure-tested in native lab, including this audit's clock/dispatch fixes | Two-host failover, target fencing and production load/SLO campaigns |
| A1–A4 — Investigation, tools, provenance, abstention | Typed bounded simulator checks and read-only prototype advisory | Real trusted telemetry provenance, calibration and independent model/tool evaluation |
| P1–P3 — UI, approvals, forensic graph | React workbench, tenant/role boundaries, immutable approval evidence, API events and opt-in traces | Real IdP/browser tests, multi-replica SSE, keyboard accessibility, collector trace propagation |
| S1 — Paired replay | Deterministic simulator twin fixtures | Controlled model-vs-runbook trial with sealed oracle |
| E1–E4 — Experience, outcomes, failure learning, memory | Opt-in capture, restricted projector, adjudication and invalidation; this audit fences forged evidence IDs | Privacy/backup erasure, per-tenant projector isolation, external oracle and independently reviewed provenance |
| E5–E8 — Evaluation, rollout, routing, adapters | Offline holdout verifier, shadow-only rollout, deterministic routing and 17 simulator adapter checks | Independently held-out E5 evidence, signed E6 release, real external adapter qualification |

**Known residual risks and next required controls:**

1. **P0 — Production authority:** G1 real IdP integration and independent human
   security/release roles remain unverified. Developer tokens never qualify.
2. **P0 — Real effects:** G2 has no authorized real target adapter. Production
   effect execution remains simulator-only by design; external fencing,
   idempotency retention, status oracle and abort behavior are unproven.
3. **P0 — Policy promotion:** G4/G5 independent blinded evaluation, frozen
   signed evidence, release-owner controls and rollback certification are
   absent; live policy assignment remains disabled.
4. **P1 — Multi-host resilience:** G3 is still missing a **Temporal**
   persistence restore, multiple independently managed hosts, true RPO/RTO
   and failure-injection timing evidence.
5. **P1 — Projector compartmentalization:** A dedicated PostgreSQL identity,
   column grants and pending-intent RLS exist. A compromised service role
   can still inspect other tenants with pending projection intents; replace
   shared service scope with independently certified tenant isolation.
6. **P1 — Privacy and audit:** Consent revocation hides old capsules at read
   time and retention scrubs the active database, but backups, exported
   telemetry and erasure lifecycle still lack independent verification.
7. **P2 — Operator workbench and observability:** Real browser/OIDC PKCE,
   accessibility, multi-replica SSE, load and trace-collector tests remain
   needed under G6.

## Release verdict

**G1–G6 remain BLOCKED.** The working simulator is more robust after this
audit; no change in this document is authority to enable production effects,
promote a policy, or assert that the final project-completion percentage is
verified. The earlier approximate development completion should be revisited
only after milestone-based acceptance evidence, not test coverage.
