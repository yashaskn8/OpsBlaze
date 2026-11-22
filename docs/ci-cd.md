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
revoked capsule, completed projection intent, migration revision and RLS
settings. It fails closed on any mismatch and uploads metadata-only evidence.
The restored database and dump are removed after the check.

This verifies **local schema/data continuity only**. It is neither a
backup-erasure certificate nor a Temporal-backup restore, multi-host RPO/RTO,
production restore, or disaster-recovery signoff. Those remain blocked in G3
and G6.
