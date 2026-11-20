# OpsBlaze red-team follow-up — 20 November 2026 (pass 3)

## Scope

Authorized, non-destructive defensive code review, adversarial Python
regression tests, synthetic signed tokens, and native PostgreSQL/Temporal
CI against **OpsBlaze's own isolated lab**. No real target, external IdP,
or live infrastructure was attacked. All changes were made directly
on `main`, with no Docker or pull requests.

This is an incremental review after the two previous red-team rounds.
Passing repository tests cannot establish zero vulnerabilities or production
approval.

## Newly addressed findings

| ID | Affected trust boundary | Defensive change |
|---|---|---|
| RT3-01 | Access-token lifetime over long-lived SSE connections | Preserve the verified JWT expiry in the authenticated principal and stop each stream at expiration, including between events in a prefetched batch |
| RT3-02 | JWS signing header parsing | Reject unrecognized `crit`/`b64` extensions, empty/oversized/nonprintable key IDs, and unsupported algorithms for both API and IdP security-event JWTs |
| RT3-03 | Held-out evaluation input integrity | Reject nonstandard JSON `NaN`/`Infinity` literals and disallow nonfinite values in canonical oracle/trial digests |
| RT3-04 | Independent postcondition witness types | Require an exact integer observed version and Boolean true health status; Python float/int and integer/Boolean equality no longer suffice for verified effect or outcome |
| RT3-05 | Idempotent final receipt replay | Recheck trusted simulator adapter identity, send count and version before returning an already stored `VERIFIED` receipt as verified; leave original history unchanged on mismatch |
| RT3-06 | Outcome-to-incident lineage | Before an independent recovery outcome, recheck exact incident target, simulator adapter, one durable send attempt, and matching original/observed versions |

## Regression coverage

Tests in `test_events_sse.py`, `test_oidc_auth.py`, `test_heldout.py`,
`test_simulator_effects.py` and `test_outcomes.py` include
synthetic token expiry, unsupported headers, malformed evaluation values,
coercible witness values, corrupted terminal receipts, and substituted
outcome lineage. Adversarial tests also run in the separate
`security-negative` CI job. Existing backend, PostgreSQL/Temporal,
Temporal smoke and frontend jobs continue to run.

## Release status

**Still blocked:** externally witnessed IdP revocation and key rollover,
external adapter qualification, true multi-host failover, independent
held-out AI outcomes, signed human release authorization and independent
penetration testing. Simulator effect execution must not be represented
as a certified third-party adapter; live candidate-policy promotion
remains disabled.

The goal of repeated audits is to reduce verified risk, not claim that
all possible bugs have been found or that production is safe.
