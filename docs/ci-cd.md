# OpsBlaze CI/CD and release safety

This repository builds a simulator-only system. All real-effect and live-policy
production gates in [the acceptance checklist](production-acceptance-gates.md)
remain BLOCKED; a green workflow does not authorize production deployment.

The main GitHub Actions workflow runs on main pushes, optional PR validation,
manual dispatch and a weekly UTC dependency/security schedule. It uses only
native Linux processes, without Docker or live infrastructure targets.

Hard-failing gates: workflow syntax and secrets checks, one complete Alembic
history, Python correctness lint and a 75% branch-aware coverage threshold,
security-negative tests and Bandit, strict pip-audit and high/critical npm
audit, strict TypeScript/Vite build, native Temporal workflow replay tests,
native PostgreSQL schema drift/conformance/E2E/downgrade tests, and CodeQL
Python and JS/TS extended security analysis.

After all checks pass on main, the release-candidate job creates a Python
wheel/sdist and frontend bundle, bundles migrations/config/ops scripts, creates
a SHA256 checksum and obtains GitHub artifact provenance attestation. It
uploads an artifact only; it does NOT deploy or activate external adapters.
All external GitHub actions are commit-SHA-pinned, checkout credentials are
not persisted, and permissions are least-privilege per job.

Repo admins must configure protected required checks, 2FA, secret-scanning
push protection and trusted Actions policy in repository settings. This
workflow cannot make those administration changes. Real CD requires a
provisioned staging/production target, OIDC-bound credentials, independent
environment approval, rollback/restore testing, release signatures, and
proof of the acceptance checklist gates. None are yet certified.

Not covered by this CI: real IdP/browser E2E, external effect fencing,
multi-host failover and disaster recovery, independent blinded evaluation,
production-scale load evidence, accessibility audit, and backup-erasure
certification. Those remain explicit production blockers.


## Native PostgreSQL restore rehearsal

The PostgreSQL CI job uses native `pg_dump`/`pg_restore` to recover a
temporary, isolated database on the same runner. A synthetic sentinel
covers the incident, verified outcome, incident/tenant consent revocations,
revoked capsule, completed projection intent, security-actor revocation,
IdP event replay ledger, and a separate synthetic UNKNOWN effect receipt
with its original fencing/version/attempt information. It also checks the
migration revision and RLS settings. It fails closed on any mismatch and uploads metadata-only evidence.
The restored database and dump are removed after the check.

This verifies **local schema/data continuity only**. It is neither a
backup-erasure certificate nor a Temporal-backup restore, multi-host RPO/RTO,
production restore, or disaster-recovery signoff. Those remain blocked in G3
and G6.

## Fail-closed source-RLS rollback

Before the full migration downgrade, CI rolls back the source-RLS migration
and connects as the limited projector role to all six source tables. Each
must return zero rows: RLS stays enabled without source-read policies even
though earlier column-level grants may remain. The isolated restore rehearsal
also verifies that revoked source records are hidden and that a forged
capsule insert is refused after the database grants are reapplied. These
checks do not certify production backup erasure or multi-host recovery.

## Pinned GitHub Actions runtime refresh

Checkout uses the official v5 full-SHA-pinned action, and artifact upload
uses official v7.0.2 (Node.js 24), also pinned by full SHA. Upload-artifact
v5 still used Node.js 20 and produced a deprecation warning; the v7
upgrade removes that known deprecated runtime without broader token permissions. The rest of the existing
SHA-pinned workflow, native PostgreSQL/Temporal testing and no-deploy
release gates are unchanged.

## Native systemd service source audit (G3)

The workflow-policy job runs `scripts/check_native_services.py`, and
Python unit tests mutate copied service units to demonstrate rejection of
root/shared projector credentials, public API binds, expanded capabilities,
unsafe filesystem settings, missing restart policy and disabled retention
timer catchup. The units cover API, Temporal dispatch, workflow worker,
projector, aged-work monitor, and bounded hourly retention. No service is
started or deployed in CI by this static audit. The independent projector
holds the restricted database password; API readiness validates a
passwordless reference and a fresh worker heartbeat. See
`deploy/systemd/README.md` for native host prerequisites and the
uncompleted multi-host acceptance campaign.

## Self-reported host heartbeat regression checks (G3)

The new `0039_worker_host_presence` migration adds a separate bounded
`worker_host_heartbeat(role, host_id, last_seen)` record for dispatcher
and incident-worker host presence. The existing role-only worker heartbeat
remains for compatibility and projector least privilege. A host ID is an
operator-configured stable identifier, not a cryptographic attestation.
Production readiness requires at least two currently reported IDs for each
role; delayed host updates cannot regress newer records. Unit tests cover
two distinct IDs, out-of-order writes, expired/future times, invalid host
identifiers, and readiness denial under missing topology. CI's native
PostgreSQL migration/rollback tests include the new schema; the external
G3 acceptance gate still requires real physical/virtual host failover,
Temporal and PostgreSQL restores, and independently witnessed RPO/RTO.

## G1 real-provider configuration preflight

`backend.security.provider_preflight` is a command-line-only, read-only
HTTPS metadata verifier with a fixed maximum document size, no redirects,
credential forwarding, proxy inheritance or bearer token usage. Its
mocked-provider regressions enforce exact issuer/JWKS pins, same-issuer
code/PKCE endpoints, accepted S256 and RS256 metadata, minimum-strength
canonical RSA JWKs and negative redirect/malformed/oversized behaviors.
CI does not call a real IdP. Passing locally does not prove access-token
issuance, secure client redirects, revocation delivery, independent
reviewer control, key rotation or G1 certification.


## External adapter G2 evidence contract tests

Python unit/security CI now exercises a source-only G2 qualification
verifier with synthetic, disposable traces and generated public/private
test-key pairs kept in test memory. It rejects altered or missing trace
bytes, unsafe redispatch of unresolved effects, forged success on stale
fences, mismatched provider digests, duplicate scenarios and signatures,
and same-key role impersonation. CI does not invoke an external provider,
create real provider credentials or enable any real effect adapter.
The `EVIDENCE_INTEGRITY_VALID_ONLY` result does not certify G2.


## Long-lived multi-host readiness capacity (G3)

Migration `0040_worker_host_age_index` indexes host role/time for the
readiness cutoff. Readiness computes fresh host counts inside PostgreSQL
instead of fetching and materializing all historical host rows; a separate
bounded maintenance batch removes stale self-reported host presence older
than seven days. Negative tests cover future/stale rows, bounded pruning,
timezone and batch-size validation, plus non-regression of current live
host presence. This is native operational scaling work, not real host
independence or failover proof.

## G2 semantic evidence red-team regression

The red-team suite now rehashes deliberately contradictory provider
traces, including swapped effect IDs, mismatched permit hashes, spoofed
VERIFIED oracle states, wrong dispatch counts, extra credential-like
fields, malformed JSON, duplicate JSON keys, reordered phases, and a
status-recovery record for a different effect. These fail even when the
outer trace digest is recalculated. A matching SHA and signed manifest
alone no longer suffice; the G2 verifier requires typed source events
that agree with the claimed scenario. This still does not independently
prove that the claimed external provider events happened.

## G1 production identity-origin red-team checks

The production authentication configuration now rejects cross-port JWKS,
issuer or JWKS credentials, query/fragment components, ambiguous path
segments and encoded slashes before any token signing-key retrieval.
The read-only IdP preflight uses the same HTTPS default-port normalization.
Regression tests check that invalid configuration fails the API closed and
never invokes a key fetch. They are not a substitute for real-provider OIDC
login, key rotation, revocation and browser PKCE campaigns.

## G4 independent witness-count inflation red team

The offline recovery-cost validator now checks unique witness references
**across both evaluated arms**, not just syntactically valid SHA-256 strings.
It also enforces 10-case minimum families, adequate negative controls and
complete frozen paired records when called directly rather than through
the E5 CLI. Exact per-family recovery-per-cost ratios flag regressions
that an aggregate success count could conceal. Synthetic regression cases
recompute the oracle digest to ensure these failures are caught by the
semantic checks and never mark a candidate promotion eligible.

## G2 evidence-file descriptor red team

The offline reviewer now opens a no-follow evidence directory once and
uses directory-relative no-follow file descriptors, enforcing ordinary
single-link files before hashing. This closes a pathname check/open race
for symlink replacement, and rejects hard-link sharing. Unit tests
simulate symlink-check bypass with byte-identical content and ensure the
reviewer still fails closed. This workflow requires native Linux.


## G5 forged-report red-team and source-chain verification

The offline three-authority G5 CLI now requires the sealed E5 inputs and
a separately pinned preregistration key. It recomputes the protected
paired holdout, signed sample binding and witness/cost summary and checks
the **entire** reviewed report, not just its supplied digest. A signed
but unsupported OFFLINE_PASS, duplicate witness, cost regression, changed
oracle, modified trial, or missing evidence fails closed. Synthetic tests
regenerate otherwise-valid reviewer signatures over a fabricated E5
report and confirm the verifier still rejects the claimed result.
The command only emits non-authorizing review evidence and never enables
live policy promotion or external effects.


## G3 undelivered Temporal start after PostgreSQL restore

The native, isolated PostgreSQL dump/restore rehearsal now seeds an
additional synthetic incident with a `START_WORKFLOW` outbox intent that
has been attempted twice but remains undelivered and replayable.
After restore the sentinel checks its original outbox and incident
identity, exact retry count, unchanged eligibility timestamp, empty
signal payload, absence of delivery/dead-letter markers, and stable
tenant/incident-derived workflow ID. The restricted projector must not
see the pending operational start.

Dedicated red-team unit tests corrupt each field (including a boolean
attempt, false delivery, altered timestamp, forged workflow identity)
and require fail-closed validation. The rehearsal **does not dispatch**
this row. PostgreSQL state continuity is not proof of restored Temporal
histories, external effect reconciliation or actual cross-host RPO/RTO.
