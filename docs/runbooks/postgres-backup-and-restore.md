# OpsBlaze Runbook: PostgreSQL Backup & Isolated Restore Drill

## 1. Context & Purpose
In OpsBlaze / TITAN-20, PostgreSQL serves as the authoritative transactional domain database. This runbook establishes standard operating procedures for taking non-blocking custom-format backups (`pg_dump -Fc`), executing isolated ephemeral restoration drills (`pg_restore`), verifying schema and data integrity, forward-migrating pre-Stage-3 snapshots to current Alembic heads (`stage3_002_b2_b3_e8`), and guaranteeing database safety without risk to live production or test databases.

---

## 2. Backup Strategy (`pg_dump -Fc`)

### 2.1 Standard Backup Procedure
Always use the PostgreSQL custom archive format (`-Fc`), which supports compressed storage, table-level selective restoration, parallel index restoration, and full schema metadata introspection:

```powershell
# Create backup directory
New-Item -ItemType Directory -Force -Path .local/backups/stage4

# Export database using pg_dump custom format
$env:PGPASSWORD = $s.postgres_password
pg_dump.exe -h localhost -p 5432 -U postgres -Fc -b -v -f .local/backups/stage4/opsblaze_$(Get-Date -Format 'yyyyMMdd_HHmmss').dump opsblaze
pg_dump.exe -h localhost -p 5432 -U postgres -Fc -b -v -f .local/backups/stage4/opsblaze_test_$(Get-Date -Format 'yyyyMMdd_HHmmss').dump opsblaze_test
```

### 2.2 Critical Safety Rules
1. **Never use plain SQL dump with `--clean` or `--create`** on shared environments.
2. **Never drop or recreate live `opsblaze` or `opsblaze_test`** during backup or restore drills.
3. **Always verify dump header and contents** before declaring a backup valid:
   ```powershell
   pg_restore.exe -l .local/backups/stage4/opsblaze_test_stage4.dump | Select-Object -First 20
   ```

---

## 3. Isolated Ephemeral Restore Drill

### 3.1 Procedure
Every recovery drill MUST restore into an ephemeral isolated database with a unique timestamp/UUID suffix:

1. **Create Ephemeral Target Database:**
   ```sql
   CREATE DATABASE opsblaze_restore_ephemeral_TIMESTAMP WITH OWNER = postgres ENCODING = 'UTF8';
   ```
2. **Execute Restoration via `pg_restore`:**
   ```powershell
   pg_restore.exe -h localhost -p 5432 -U postgres -d opsblaze_restore_ephemeral_TIMESTAMP -v --no-owner --no-privileges .local/backups/stage4/opsblaze_test_stage4.dump
   ```
3. **Inspect Schema & Row Fingerprint:**
   Verify tables (`tenant`, `incident`, `outbox`, `effect_receipt`, `resource_epoch`, `resource_reservation`, `adapter_conformance`, `alembic_version`), primary keys, unique constraints, foreign keys, and row counts.
4. **Forward-Migration Verification (if restoring a legacy pre-Stage-3 dump):**
   ```powershell
   $env:DATABASE_URL = "postgresql+asyncpg://postgres:secret@localhost:5432/opsblaze_restore_ephemeral_TIMESTAMP"
   alembic upgrade head
   ```
5. **Teardown Ephemeral Database:**
   Terminate any lingering connections and drop the ephemeral database:
   ```sql
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'opsblaze_restore_ephemeral_TIMESTAMP';
   DROP DATABASE opsblaze_restore_ephemeral_TIMESTAMP;
   ```

---

## 4. Automated Verification Script
Run the validated test and automation script:
```powershell
.\.venv\Scripts\pytest.exe tests/integration/test_stage4_postgres_migration_and_restore.py -v
```
Results and verified schema fingerprints are preserved in [`docs/evidence/stage4-postgres-restore-drill.json`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/evidence/stage4-postgres-restore-drill.json).
