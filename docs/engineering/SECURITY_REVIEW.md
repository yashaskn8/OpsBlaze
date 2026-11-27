# Security & Privacy Review

## 1. Authentication, Authorization & Tenant Isolation
- **Fail-closed API matrix** ([backend/api.py](../../backend/api.py), [backend/auth.py](../../backend/auth.py)): Verified via [tests/test_api_authorization_matrix.py](../../tests/test_api_authorization_matrix.py) and [tests/test_openapi_contract.py](../../tests/test_openapi_contract.py). Every mutating endpoint requires `local_tenant` plus an explicit role (`local_operator`, `local_reviewer`, or `local_security_admin`), except `/v1/security/idp-events` which enforces a separate audience-bound signed IdP JWT.
- **Reviewer/Operator Separation**: Development mode requires separate `OPSBLAZE_LOCAL_TOKEN` and `OPSBLAZE_REVIEW_TOKEN`; production mode enforces distinct OIDC roles, revoked-identity lookups (`RevokedIdentity`), and signed IdP replay fencing (`IdentityProviderEvent`).
- **Workbench Identity & Incident Fencing**: Verified at `9ecdaf4` that tenant/incident switches clear prior scoped state before paint and cancel stale fetches.
- **HTTP Security Headers Repair (`b0984ad`)**: Added `@app.middleware("http") _apply_security_headers` in [backend/api.py](../../backend/api.py) setting `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on `/v1/` routes without overriding `Cache-Control: no-cache` on `/v1/incidents/{id}/stream`.

## 2. Database Privilege Separation & Privacy Controls
- **`opsblaze_projector` Restricted Role** ([scripts/projector_grants.sql](../../scripts/projector_grants.sql), [tests/test_pg_projector_role.py](../../tests/test_pg_projector_role.py)): Non-superuser, non-owner, no `BYPASSRLS`; column-limited `SELECT` on `incident`, `outcome_revision`, and `evidence`; PostgreSQL RLS denies access to `START_WORKFLOW` outbox rows, source secrets/excerpts, and forged capsules.
- **A3-08 Evidence Redaction, Expiry & Access Audit** ([backend/probes/gateway.py](../../backend/probes/gateway.py), [backend/workers/retention.py](../../backend/workers/retention.py)): `redact_evidence_excerpt` strips Bearer tokens, `sk-...` keys, credential pairs, and PEM private keys before commit; `expires_at` (`30d`) blocks assessment (`EXPIRED_EVIDENCE`), suppresses excerpts before async scrub (`5e7a329`), and scrubs payloads via `expire_evidence_payloads` while preserving forensic SHA-256 history and `EvidenceAccessAudit` trails.

## 3. AI Tool Boundary & Control-Plane Hardening
- **No Arbitrary Execution**: `ALLOWED_PROBES` permits only `simulator.health` and `simulator.version`; `arbitrary_execution_enabled` is fixed to `False`; read-only advisory schemas use `ConfigDict(extra="forbid")` (`ReadOnlyAdvice`).
- **Telemetry Safety** ([backend/telemetry.py](../../backend/telemetry.py)): Exports only allowlisted UUIDs, outbox IDs, and scheduling latency (`record_exception=False`, `set_status_on_exception=False`).

## 4. Supply-Chain, Static Analysis & Residual Findings
- `bandit -q -ll -r backend`: **0 medium/high issues** (4 pre-existing low-severity `B101`/`B105`/`B106` items in local benchmark/preflight/retention modules).
- `scripts/check_secrets.py`: **Checked 288 tracked paths**, 0 findings.
- `npm --prefix frontend audit --omit=dev`: **0 vulnerabilities**.
- CodeQL Python & JS/TS security scans pass in GitHub Actions CI ([45848362323](https://github.com/yashaskn8/OpsBlaze/actions/runs/45848362323)).
- **Residual Risks**: Development shared tokens remain non-production credentials; real external IdP (`G1`), external adapter privacy (`G2`), and penetration/backup purge audits (`G6`) remain `BLOCKED`.
