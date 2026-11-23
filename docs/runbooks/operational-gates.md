# OpsBlaze operator runbooks — simulator/lab and release gates

These procedures accompany the TITAN-20 Volume V operations requirements.
They **do not authorize production effects, a new policy, or recovery writes**.
Only approved operators may inspect their own tenant; security/release owners
must approve any consequential operational change. Keep raw incident evidence,
tokens, and identity-provider subject values out of public tickets and logs.

## Stalled incident or lost Temporal start ACK (B1/G3)

- **Trigger:** queue-age or stale-state alert, delayed START_WORKFLOW outbox,
  missing worker progress, or G3 audit finding.
- **Owner:** on-call platform operator; escalation to workflow owner.
- **Inspect:** record UTC observation time, incident ID, stable workflow ID,
  outbox ID, attempted/ACK state, PostgreSQL revision, Temporal status,
  and whether worker/dispatcher host presence is healthy. Use:

  ```bash
  python -m backend.workers.reconciler --stale-seconds 300
  python -m backend.workers.recovery_audit --stale-seconds 300 --limit 20
  ```

  A recovery audit exit status of 1 means findings; 2 means the sample was
  incomplete. With a `next_after_id`, inspect subsequent pages using
  `--after-id <outbox-row-id>`. Do not interpret a single clean page as a
  complete audit. A Temporal timeout or missing history is **UNKNOWN**, never
  proof that the workflow was not started.
- **Safe action:** preserve the original durable outbox intent and immutable
  workflow ID. Investigate service health and restart only the approved
  *service process* through the established operations procedure. Never
  delete a start intent, invent an ACK, or dispatch a second workflow ID.
- **Evidence:** store redacted before/after audit JSON, process/host identity,
  deployment digest, status timestamps, and any human reconciliation decision.

## Unknown or unverified external effect (B2/B3/E8)

- **Trigger:** `UNKNOWN`, `SENT`, stale `CONFIRMED`, invalid receipt,
  or inability to fetch independent postconditions.
- **Owner:** effect-gateway operator plus adapter/security owner.
- **Inspect:** match original incident, approved plan digest, effect ID,
  idempotency identity, fence, expected target version and immutable receipt.
  Simulator operators can use the existing status-only reconciliation API
  `POST /v1/effects/{effect_id}/reconcile` with an authorized session.
- **Safe action:** status inquiry for the *same* effect identity only.
  Reconciliation must not issue a new apply. If status or postcondition
  cannot be proved, leave the receipt unresolved and escalate to a human.
  A local receipt alone never proves recovery; never blindly retry a mutation.
- **Evidence:** independent status source, observed target version,
  receipt transitions, adapter code digest and reviewer identity (pseudonymous).

## Recovery oracle or model unavailable (A1/A4/E2)

- **Trigger:** missing/ambiguous recovery witness, probe budget exhausted,
  model timeout, invalid schema, or uncertain evidence.
- **Owner:** incident commander / model evaluation owner.
- **Safe action:** preserve inconclusive/abstained state. Use the approved
  deterministic runbook or handoff; do not fabricate a VERIFIED outcome.
  Do not promote learning candidates from disputed or unknown outcomes.
- **Evidence:** pinned incident/evidence versions, budget state, probe denial
  reason, separate witness status, and a human review disposition.

## Consent revoked or experience data needs erasure (E1/E4)

- **Trigger:** consent withdrawal, tenant privacy request, expired capsule,
  invalidated outcome or tainted evidence.
- **Owner:** tenant privacy/security owner.
- **Safe action:** check the current consent epoch and enforce read-time
  invalidation **before** relying on asynchronous expiry cleanup.
  Run only the authorized bounded native maintenance process and verify that
  retrieval rejects old epochs. Active-database scrubbing alone does not
  establish erasure from backups, traces or downstream exports.
- **Evidence:** pseudonymous request reference, epoch/version changes,
  before/after retrieval-denial tests, retention job outcome, backup policy.

## Candidate fails evaluation or canary safety gate (E5/E6)

- **Trigger:** regression, safety violation, invalid holdout provenance,
  failed signing check, or hypothetical shadow-risk indicator.
- **Owner:** independent evaluator, security reviewer and release owner.
- **Safe action:** retain the pinned known-good incumbent. Pause/rollback
  exposure only under an independently approved release procedure; never
  automatically turn a simulator PASS into a live rollout. Existing pinned
  incidents are not silently reassigned or undone.
- **Evidence:** frozen input digests, preregistration, negative controls,
  per-family outcomes, signatures, rollback decision and timestamps.

## Database or Temporal restore (G3)

- **Trigger:** data loss, corrupted history, suspected partial restore or
  verified recovery exercise.
- **Owner:** database/platform SRE with an independent security approver.
- **Safe action:** conduct first in an isolated non-production environment
  with **all mutation paths disabled**. PostgreSQL and Temporal backups
  are not automatically a consistent cross-store snapshot. Verify schema
  migrations, IdP revocations/replay ledger, consent tombstones, approval
  grants, pending starts, stable workflow IDs, unresolved receipts and
  capsule read isolation. Reconcile every ambiguity before any human
  decision to re-enable mutation paths. Do not use the synthetic
  `scripts/pg_restore_smoke.py` result as proof of a real restore.
- **Evidence:** backup inventory, source/restore timestamps, versions,
  redacted logs, RPO/RTO measurements, approval records and unresolved risks.

## Drill acceptance record

For every drill record: scenario, owner, UTC time, exact source/adapter
versions, prerequisites, fault injection, expected safe invariant, observed
behavior, raw metrics/latency percentiles, failing negative controls,
recovery decision, independent reviewer and artifact digest. A drill is
**not accepted** when it lacks independently witnessed evidence. See
[`docs/production-acceptance-gates.md`](../production-acceptance-gates.md)
for the unclosed G1–G6 release criteria.
