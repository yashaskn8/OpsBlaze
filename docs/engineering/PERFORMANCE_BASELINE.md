# Performance & Query Plan Baseline

## TITAN-20 v2.0 Community candidate

The Windows working tree based on `c88065af312e2d2ea52150e6b47e1e69c9baaed8` ran `python -m backend.benchmarks.local_capacity` with Python 3.12.10 on an Intel Core i7-10870H with 16 logical processors. This was an in-process FastAPI and SQLite run during other local verification, with no network between the benchmark caller and API.

| Operation | Samples | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Admission | 12 | 33.804 ms | 120.673 ms | 120.673 ms |
| Incident snapshot | 36 | 13.854 ms | 32.376 ms | 40.028 ms |
| Notifications | 36 | 18.525 ms | 35.659 ms | 37.646 ms |
| Overload rejection | 4 | 49.767 ms | 52.390 ms | 52.390 ms |

All four overload requests received HTTP 429, and the store retained the 12 admitted incidents and their 12 durable start intents. The React/Vite production build measured 339.88 kB JavaScript and 100.26 kB gzip on Node 24.11.1. PostgreSQL workload latency, browser interaction timing and performance on independent hosts require distinct measurements. The measured strategy, memory and recovery-cost comparisons remain below with their original source checkpoints and scope.

The focused synthetic comparison at `556088d0391d608f6f191150b20b82c492e9f504` recorded 6 correct decisions out of 6 in each A1 arm and zero unsafe proposals. Adaptive and cheapest-first each used 9 probes. Fixed runbook used 11. The E4 ablation used design digest `fcff2a5e8f2189b9d5218516d01bfcb0be99b8f51b4bdb28ab14a88bbf756985`. Both arms made 6 correct decisions out of 6 and used 10 probes. In this SQLite run, median investigation latency was 43.725 ms with memory and 44.927 ms without. The corresponding p95 values were 53.365 ms and 53.354 ms. That sample leaves the effect on latency and decision quality inconclusive. Each E7 strategy made 6 correct decisions out of 6. Adaptive, cheapest-first and default runbook each used 5 probes, and fixed runbook used 6. Model version, provider latency and cost require an actual provider run. Independent efficacy remains a G4 requirement.

All measurements below were collected on the native Windows PostgreSQL 16.16 instance (`127.0.0.1:55435`), local Python `.venv`, and in-process FastAPI ASGI transport. They are reproducible development baselines, not multi-host production SLOs (`G3`/`B4-07`).

## 1. Native PostgreSQL 16 `EXPLAIN (ANALYZE, BUFFERS)` Baseline (Post-Restore DB)

| Query Target | Plan Summary | Planning Time | Execution Time |
|---|---|---:|---:|
| `incident` (`tenant_id, status_projection ORDER BY created_at DESC LIMIT 20`) | `Seq Scan on incident` (`rows=0` on sentinel DB) | `4.087 ms` | `0.110 ms` |
| `evidence` (`tenant_id, incident_id ORDER BY captured_at DESC LIMIT 50`) | `Seq Scan on evidence` (`rows=0` on sentinel DB) | `3.899 ms` | `0.148 ms` |
| `outbox` (`delivered_at IS NULL AND dead_lettered_at IS NULL ORDER BY available_at, id LIMIT 10`) | `Seq Scan on outbox` (`rows=3`) | `5.752 ms` | `0.193 ms` |
| `operational_lesson` (`tenant_id, target_digest, status = 'ACTIVE' LIMIT 4`) | **`Index Scan using ix_lesson_lookup`** | `2.601 ms` | `0.095 ms` |

## 2. In-Process FastAPI ASGI Endpoint Latency (`n = 30` per endpoint, `b0984ad`)

| Endpoint | Samples | p50 (ms) | p95 (ms) | p99 (ms) | Notes |
|---|---:|---:|---:|---:|---|
| `GET /v1/health/ready` | 30 | `4.674` | `6.374` | `677.004` | First-call Temporal fail-fast accounts for p99 cold tail |
| `POST /v1/incidents` | 30 | `16.969` | `20.224` | `57.206` | Includes atomic admission + quota + outbox commit |
| `GET /v1/incidents/{id}` | 30 | `7.089` | `8.428` | `9.044` | Scoped single-row snapshot |
| `GET /v1/incidents/{id}/evidence/lifecycle` | 30 | `10.931` | `25.644` | `68.745` | Aggregate freshness, citations & bounded access audits |

## 3. Historical & Comparative Engineering Measurements (from Audit & Phase Commits)
- **E4 Scoped Memory Lookup Optimization (`bb96140`)**: Combined capsule, tenant/incident consent, and latest outcome checks reduced eligible PG16 lookup from **7 SQL statements to 3** (`4.384 ms` active-lesson median vs `5.035 ms` prior).
- **B4 Database Readiness Timeout (`e245b42`)**: Under injected SQL stall, `GET /v1/health/ready` returned HTTP `503` cleanly within the bounded readiness deadline (test-budget median `158.1 ms`, range `155.5–159.5 ms` vs unbounded `412.5 ms` prior; runtime DB phase deadline `3s`).
- **Scale & SSE Load Measurements ([docs/requirements-audit.json](../requirements-audit.json))**: Mixed lineage PG16 (`120 evidence + 60 hypotheses`, 4 readers): `summary_p95 = 141.657 ms`, `page_p95 = 27.601 ms`; SSE fan-out: `p95 = 184.081 ms`, `peak_memory = 1779.83 KiB`; overload & drain PG16 (`tenant_cap=8`): `429` rejections `p95 = 30.334 ms`.
- **Comparative Simulators**:
  - **A1-10** (`run_a1_strategy_comparison`): `ADAPTIVE` & `CHEAPEST_FIRST` `9` probes (`6/6`, `0` unsafe, `0` target mutations, `p50=40.0ms`) vs `FIXED_RUNBOOK` `11` probes (`6/6`).
  - **E5-05** (`run_e5_paired_recovery_cost`, 36 pairs SQLite / native Temporal): Native Temporal run (`0aae1c0`/`7fa9f69`) recorded `36/36` correct, `22` verified recoveries, `1` unknown, `13` nonrecoveries, `56` adaptive vs `64` runbook probes (`p95 = 556 ms` vs `564 ms`).
