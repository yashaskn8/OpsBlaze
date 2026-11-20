# OpsBlaze red-team follow-up — 20 November 2026 (pass 2)

## Scope

Authorized, non-destructive review of OpsBlaze's isolated simulator,
synthetic incident evidence, opt-in memory metadata, approvals, receipt
state machine, Temporal outbox, and native PostgreSQL CI. No real
infrastructure, external identity provider or target service was attacked.

This is an incremental follow-up to `docs/red-team-2026-11-20.md`.
Findings were validated with regression tests and reviewed in CI. A
green test run is **not** external penetration certification.

## Findings and mitigations

| ID | Finding | Mitigation and verification |
|---|---|---|
| RT2-01 | Source capsules could be relabeled consistently with their lessons to a different target without recomputing the digest from their originating incident. | Recompute the incident/tenant target digest at `valid_source` and `list_capsules` read time; negative tests in `test_lessons.py` and `test_experiences.py` |
| RT2-02 | Repeated experience capture could return a previously valid capsule after the consent epoch changed or source metadata was altered. | Reject stale epoch, altered target digest, expired or revoked capsules on reuse; regression tests |
| RT2-03 | Draft lesson validation rechecked rule ID but not the exact allowlisted summary, future start or expiry past the source. | Bind summary to allowlist and lesson window to active source retention; negative tests |
| RT2-04 | Cached positive A4 assessments were returned for the same evidence-ID list after source edits, target changes or later denied probes. | Revalidate target, probe lineage, trusted source metadata, hash, freshness and denied-tool history at read time; preserve immutable original decision but return no reusable positive result |
| RT2-05 | Initial A4 and approved plans did not consistently require simulator source-version and internal classification metadata. | Check both fields before assessment, approval and dispatch; source metadata swap regressions |
| RT2-06 | A receipt could be confirmed or verified with an impossible version through the low-level setter. | Fail closed at the transition itself; separate direct persistence-tamper recovery test |
| RT2-07 | A PREPARED/UNKNOWN receipt without a durable SENT attempt could later appear confirmed after an external status reply. | Require exactly one durable send attempt for every nonterminal post-send receipt; never promote a no-send receipt from a remote status lookup |
| RT2-08 | The Temporal PLAN_DECIDED outbox bridge accepted non-Boolean values or extra fields in signal payloads. | Strictly validate canonical UUID, exact payload keys and true Boolean decision before signaling |

## Regression coverage and release gates

- Synthetic cross-target and stale-consent capture replays
- Poisoned lesson text and source-unbounded validity windows
- Post-assessment evidence mutation, future timestamps, source metadata
  substitution and late denied probes
- Confirmation version jump, forged zero-send receipt, corrupted
  confirmed version and status-only reconciliation
- Malformed or truthy-string Temporal decision payloads
- Native PostgreSQL/Temporal and frontend smoke jobs remain separate
  from SQLite/API and dedicated security-negative jobs

New adversarial tests use the `redteam` name where relevant to appear
in the dedicated security-negative CI job as well as full backend tests.

**Unresolved external requirements:** independent real IdP testing, real
target adapter qualification, multi-host failover, independent held-out
AI evaluation, release-owner cryptographic signing, disaster recovery,
human penetration testing and deployment hardening remain blocked.
The repo does not enable external effects or live policy promotion.
There is no meaningful claim that every possible exploit has been fixed.
