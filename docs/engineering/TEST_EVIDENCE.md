# Verification & Test Evidence Ledger

## TITAN-20 v2.0 working-tree evidence

The Community candidate started from `c88065af312e2d2ea52150e6b47e1e69c9baaed8`. Local verification ran 791 Windows-compatible backend tests plus a targeted run of 11 profile and ledger tests, including the launcher checks introduced after full-suite collection. Combined coverage was 78.97% across 8,368 statements and 2,670 branch opportunities. The nine documented POSIX-only cases remained in Linux CI.

Native Windows PostgreSQL 16.16 passed 41 integration cases, the no-drift check, restricted grants, the restore sentinel and downgrade. Temporal passed 12 separate native integration and replay cases. The PostgreSQL run retained a pre-existing asyncpg cancellation-cleanup warning. The frontend passed 17 contract tests and the production build. Isolated Playwright MCP verified keyboard navigation and rejection at 390 px. Spoken output remains pending.

The profile tests reject Enterprise selection, unknown profiles, a missing frontend build and a public host argument. They exercise the existing authorization route through the same-origin launcher. The generated requirement report preserves all 176 atomic IDs, records C1–C6 separately and derives applicability and acceptance totals from the ledger. Actionlint, Ruff, Bandit, Import Linter, Dependency Cruiser and the native service source check passed.

All counts below come directly from executed runs on the local repository (`main` at `7c2d567` / `b0984ad`).

## 1. Executed Test Suites

| Check / Suite | Exact Command | Result | Notes / Exclusions |
|---|---|---|---|
| **OpenAPI, Security Headers & Auth Matrix** | `.venv\Scripts\python.exe -m pytest -q tests/test_openapi_contract.py tests/test_b2_reservation_invariants.py tests/test_api_contract.py tests/test_api_authorization_matrix.py tests/test_simulator_effects.py tests/test_adapter_conformance.py tests/test_events_sse.py` | **`73 passed in 15.58s`** | Added 7 new contract & B2 stateful property tests (`b0984ad`) |
| **Native PostgreSQL 16 Integration (`pg_e2e`)** | `pg16_omega_drill.py` running `pytest -q -m "integration and pg_e2e" tests/test_pg_projector_role.py tests/test_pg_redrive_concurrency.py tests/test_pg_two_worker_failover.py tests/test_pg_restore_sentinel.py` | **`3 passed, 17 deselected in 5.66s`** | Full Windows/CI PG suites also run in Linux CI (`45848362323` passed all 10 jobs) |
| **Native Temporal Integration & Replay** | `.venv\Scripts\python.exe -m pytest -q -m "integration or not integration" tests/test_temporal_history_replay.py tests/test_temporal_worker_restart.py tests/test_temporal_dispatch_recovery.py tests/test_temporal_signal_idempotency.py tests/test_temporal_workflow.py` | **`9 passed in 12.14s`** | Includes pinned source replay & worker restart across phases |
| **Windows Full Non-Integration Backend Suite** | `.venv\Scripts\python.exe -m pytest -q` (at `b0984ad`) | **`770 passed, 49 deselected in 119.73s`** | Deselected default `integration` marker + 9 POSIX symlink/hardlink `windows_only_exclusions` |
| **Frontend Node Contract Tests** | `node --test frontend/tests/*.test.mjs` | **`17 passed, 0 failed`** | Stubbed `fetch` HTTP & SSE parser/paging/diagnostics invariants |
| **Frontend Production Build** | `npm --prefix frontend run build` (`tsc --noEmit && vite build`) | **PASS** | `37` modules transformed, `dist/assets/index-DlHWJ9j4.js` `339.42 kB` |
| **Migrations & Secrets** | `.venv\Scripts\python.exe scripts/check_migrations.py` & `scripts/check_secrets.py` | **PASS** | `54 migrations; head: 0054_evidence_lifecycle`; `Checked 288 tracked paths` |
| **Architecture & Static Gates** | `lint-imports.exe`, `depcruise`, `ruff check backend tests --select F,W,B023,ISC004`, `bandit -q -ll -r backend`, `npm --prefix frontend audit --omit=dev` | **PASS** | `6 kept, 0 broken`; `0` depcruise violations; `0` Bandit medium/high; `0` npm vulns |

## 2. Unavailable Optional Tools (`BLOCKED` in Current `.venv`)

| Check | Status | Reason & Substituted Coverage |
|---|---|---|
| **Hypothesis CLI / library** | `BLOCKED` | `hypothesis` is not installed in locked `.venv`. Deterministic randomized state-machine tests (`random.Random(2027)`, 120 transitions) in `tests/test_b2_reservation_invariants.py` cover stateful fencing, renewal, release, expiry, and `apply`. |
| **Schemathesis CLI** | `BLOCKED` | `schemathesis` is not installed in locked `.venv`. OpenAPI 3.x validation, unauthenticated refusal, and response header contracts run in `tests/test_openapi_contract.py` + `tests/test_api_authorization_matrix.py`. |
| **9 Windows-excluded Linux tests** | `BLOCKED` (Windows) / `VERIFIED` (Linux CI) | POSIX symlink/hardlink qualification & oversize archive checks in `tests/test_external_adapter_qualification.py` (8) and `tests/test_replay_archive.py` (1) run in Linux GitHub Actions CI (`45848362323`, `45821688845`). |
| **Manual screen-reader AT (`P2-08`)** | `BLOCKED` | Automated Playwright keyboard/focus/ARIA checks passed; manual Windows Narrator spoken-output acceptance is deferred to UI redesign ([README.md](../../README.md)). |
