# Database Reliability & Disaster Recovery Drill Report

## 1. Native PostgreSQL 16 Backup & Restore Drill (Measured `2026-11-27`)

Executed against disposable native PostgreSQL 16.16 (`127.0.0.1:55435`) at commit `b0984ad`:

| Phase | Command / Action | Measured Duration | Result |
|---|---|---:|---|
| Migration upgrade (`0001 -> 0054`) | `alembic upgrade head` | `3,415.56 ms` | `0054_evidence_lifecycle` active |
| Schema drift check | `alembic check` | `1,882.17 ms` | Zero drift detected |
| Rollback & re-upgrade verification | `alembic downgrade 0053_eval_canary_adapters` -> `upgrade head` | `1,550.87 ms` + `1,507.35 ms` | Clean reversible DDL |
| Native PG concurrency & failover tests | `pytest -m "integration and pg_e2e"` (`test_pg_projector_role`, `test_pg_redrive_concurrency`, `test_pg_two_worker_failover`, `test_pg_restore_sentinel`) | `7,686.06 ms` (`5.66s` pytest) | `3 passed` (`pg_e2e` subset) |
| Seed synthetic restore sentinel | `scripts/pg_restore_smoke.py seed` | Included in drill | Revocations, `UNKNOWN` effect, `START_WORKFLOW` pending intent |
| Logical backup (`pg_dump --no-owner --no-acl`) | `pg_dump.exe -U opsblaze -d opsblaze` | `337.39 ms` | **`105,587 bytes`** SQL archive |
| Fresh database restore (`psql`) | `psql.exe -d opsblaze_restore_omega -v ON_ERROR_STOP=1` | `1,596.65 ms` | Clean schema + data load |
| Restricted role grant & trigger lock reapplication | `psql.exe -d opsblaze_restore_omega -f scripts/projector_grants.sql` | `153.73 ms` | `opsblaze_projector` grants & 2 outbox RLS policies restored |
| Post-restore invariant verification | `scripts/pg_restore_smoke.py verify` | `3,807.40 ms` | **`PASS` (`production_certified: false`)** |

- **Measured Single-Host Restore + Regrant Time (Synthetic RTO)**: **`1,750.38 ms`** (`psql_restore_ms` + `regrant_ms`).
- **Measured Data Loss on Restored Sentinel Records (RPO)**: **0** — preserved `revocation_rows=2`, `identity_revocation_preserved=true`, `idp_event_replay_fence_preserved=true`, `unresolved_effect_state_preserved="UNKNOWN"` (`attempt_count=1`), `capsule_status="REVOKED"`, `pending_temporal_start_preserved=true` (`attempt_count=2`), `rls_tables_preserved=7`, `forged_capsule_denied_after_restore=true`, and verified pending Temporal starts remained hidden from `opsblaze_projector`.

## 2. Native Temporal Workflow Replay & Interruption Recovery

Executed `.venv\Scripts\python.exe -m pytest -q -m "integration or not integration"` across `test_temporal_history_replay.py`, `test_temporal_worker_restart.py`, `test_temporal_dispatch_recovery.py`, `test_temporal_signal_idempotency.py`, and `test_temporal_workflow.py`:
- Result: **`9 passed in 12.14s`**.
- Verified pinned source history replay without nondeterminism errors, worker shutdown/restart across `INVESTIGATING`, `AWAITING_APPROVAL`, and `EXECUTING`, duplicate signal idempotency, and outbox crash recovery without repeating applied effects.

## 3. Conditional Limits (`G3` Remains `BLOCKED`)
- PostgreSQL logical dump/restore does **not** restore Temporal server persistence cleanly as a single cross-store atomic snapshot; restore drills must reconcile outbox intents against `recovery_audit.py` before enabling effects.
- Windows single-host PG16 + Temporal tests do **not** certify physical multi-host HA, real WAN partitions, or externally witnessed failover campaigns (`G3`).
