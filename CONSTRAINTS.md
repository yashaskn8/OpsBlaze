# Constraints

Last reviewed: 2026-11-27

## Floor (always enforced)

- No new suppression comments (`@ts-ignore`, `eslint-disable`, `# noqa`, `# type: ignore`) without an inline rationale and owner.
- No unimplemented stubs (`NotImplementedError`, empty `catch {}`) on production or gated code paths.
- No skipped or deleted tests without a documented reason and exception row.
- No secrets, credentials, `.env` files, or raw incident evidence tracked in Git.
- Never weaken G1–G6 production gates, authorization dependencies, PostgreSQL RLS, or Temporal determinism to pass a check.

## Enforced with numbers

| Dimension | Rule | Checked by | Runs at |
|---|---|---|---|
| Migrations | Single linear head (`0054_evidence_lifecycle`), zero schema drift | `.venv\Scripts\python.exe scripts/check_migrations.py` & `alembic check` | Every commit & CI |
| Tracked secrets | Zero matched key/token patterns or tracked `.env` files | `.venv\Scripts\python.exe scripts/check_secrets.py` | Every commit & CI |
| Compile sanity | Zero Python bytecode syntax errors | `.venv\Scripts\python.exe -m compileall -q backend` | Every commit |
| Lint correctness | Zero `F`, `W`, `B023`, `ISC004` violations | `.venv\Scripts\ruff.exe check backend tests --select F,W,B023,ISC004` | Every commit & CI |
| Security static scan | Zero medium/high Bandit findings (`-ll`) | `.venv\Scripts\bandit.exe -q -ll -r backend` | Every commit & CI |
| Python architecture | 6 forbidden import contracts kept, 0 circular imports | `.venv\Scripts\lint-imports.exe` & `codegraph cycles` | Every commit |
| Frontend types & build | Zero TypeScript errors, clean Vite production bundle | `npm --prefix frontend run build` | Every commit & CI |
| Frontend tests | All 17 Node contract & state tests pass | `node --test frontend/tests/*.test.mjs` | Every commit & CI |
| Frontend architecture | Zero `depcruise` rule violations (cycles, orphans, layer boundaries) | `depcruise --config frontend/.dependency-cruiser.cjs frontend/src` | Every commit |
| API auth & OpenAPI | Every `/v1/` route enforces tenant/role auth and security headers | `.venv\Scripts\python.exe -m pytest -q tests/test_api_authorization_matrix.py tests/test_openapi_contract.py` | Every commit & CI |
| Ledger traceability | Bidirectional traceability across 20 capabilities & 176 items | `.venv\Scripts\python.exe -m pytest -q tests/test_requirements_ledger.py` | Every commit & CI |
| Frontend deps | Zero high/critical runtime npm vulnerabilities | `npm --prefix frontend audit --omit=dev` | CI & release check |

## Measured, not yet enforced

| Metric | Baseline | Direction | Notes |
|---|---|---|---|
| Full Ruff style backlog | 929 pre-existing items (`SIM117`, `I001`, `UP017`, `B008`, etc.) | Must not grow | Correctness rules (`F,W,B023,ISC004`) enforced at 0 |
| Backend branch coverage | ~78.75% (measured at `ec109be`) | Must not regress | Tracked in Linux CI coverage gate |
| Frontend JS bundle | 339.42 kB raw / 100.22 kB gzip (`vite build`) | Bounded | Measured at `e245b42` |

## Exceptions

| ID | Scope | Reason | Owner | Review |
|---|---|---|---|---|
| E1-WIN | 9 Linux symlink/hardlink qualification & archive tests | Windows host lacks POSIX symlink/inode parity (`windows_only_exclusions`); executed in Linux CI | Repo maintainer | Until Linux runner only |
| E2-ENV | `hypothesis`, `schemathesis` CLI suites | Optional packages not installed in `.venv`; deterministic randomized B2 state-machine (`tests/test_b2_reservation_invariants.py`) & `tests/test_openapi_contract.py` cover core invariants | Repo maintainer | Next env update |
| E3-EXT | Production gates G1–G6 & manual screen-reader spoken-output AT (`P2-08`) | External IdP, qualified external target adapter, multi-host HA, protected G4 corpus, human release keys, and Narrator AT remain external/manual blockers | Security & release owners | Before live deploy |
