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
