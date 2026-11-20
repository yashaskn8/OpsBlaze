# OpsBlaze

Controlled incident investigation and recovery lab, guided by the TITAN-20 engineering blueprint (formerly RehearsalGate).

**Status:** B1 *incident admission* is implemented; Temporal dispatch, durable workflow execution, approvals, recovery actions, and the remaining capabilities are **not implemented**. This is not production-ready.

## Current vertical slice

- FastAPI incident create/read API.
- Tenant-scoped idempotency key and canonical request hash.
- PostgreSQL transaction creates one incident plus one pending outbox intent.
- Reusing the key and changing the payload returns HTTP 409.
- Local-only shared-token guard (not production authentication).
- Alembic migration and SQLite-backed unit/API tests.

Outbox entries are **not dispatched yet**. The next B1 step is a separately run Temporal dispatcher and version-safe incident workflow, with restart/crash tests. Until then, incidents remain in `DISPATCH_PENDING`.

## Local setup (no Docker)

Requirements: Python 3.11+, PostgreSQL running natively.

```bash
python -m venv .venv
# Activate .venv using your shell's activation command.
python -m pip install -e ".[dev]"
# Copy .env.example to .env and edit the PostgreSQL URL and local token.
alembic upgrade head
uvicorn backend.api:app --reload
```

Check `http://127.0.0.1:8000/docs` for interactive API docs.

Example local request:

```bash
curl -X POST http://127.0.0.1:8000/v1/incidents \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: demo" \
  -H "X-OpsBlaze-Token: YOUR_LOCAL_TOKEN" \
  -H "Idempotency-Key: sample-001" \
  -d '{"target_id":"checkout"}'
```

The same tenant + key + payload returns the same incident. A changed payload returns 409. Read it using `GET /v1/incidents/{id}` with the same tenant and token headers.

Run tests using `python -m pytest -q`; no PostgreSQL instance is needed for the local test suite.

## Safety and architecture

The local header token is strictly a development placeholder and the API deliberately refuses to accept tenant requests when `OPSBLAZE_ENVIRONMENT=production`. Do not expose this application publicly until real tenant authentication, authorization, rate limits, and observability are built.

The blueprint requires FastAPI + PostgreSQL outbox + Temporal as the B1 lifecycle; only the first transactional boundary is present so far. See `AGENTS.md` for coding constraints. No Docker, Kafka, Redis, or additional microservices are required for this slice.

## Operator UI

Run `cd frontend`, `npm install`, then `npm run dev` while FastAPI is running on port 8000. Open `http://127.0.0.1:5173` and enter the local token from `.env`. The UI supports incident admission and status reads only.

## B1 Temporal worker and dispatcher

The dispatcher consumes the transactional outbox, starts a stable Temporal
workflow ID and retries after lost responses. It runs separately from FastAPI.
The worker records lifecycle projections and waits for operator acknowledgment
or cancellation; it never performs a real remediation action.

Install a native Temporal dev server or Temporal CLI (no Docker). After starting
the server with persistent local storage, run these commands in separate terminals:

```bash
python -m backend.workers.dispatcher
python -m backend.workers.incident_worker
```

With a valid local token you can cancel an incident by sending
`POST /v1/incidents/{id}/cancel` with the current numeric `If-Match`
version returned by GET. The durable dispatcher delivers this request to
Temporal. A status response is a lagging projection; Temporal history is
authoritative. Real approval, the guarded action gateway and evidence
collection are not yet implemented.

Review acknowledgment is available at POST /v1/incidents/{id}/acknowledge
with If-Match set to the latest status version while the state is
AWAITING_APPROVAL. This closes the workflow as NEEDS_REVIEW and is NOT
approval for a real mutation. The operator UI exposes this action and cancellation.

## B2/B3 simulator correctness components

The repository now includes resource epochs, reservation fencing, a local
stateful simulator adapter, and durable effect receipts. The simulator checks
its fence and target version immediately before applying a local state change.
Receipts transition through PREPARED, SENT, UNKNOWN, CONFIRMED, and VERIFIED
(or an explicit failure/review state). Reconciliation only queries the adapter;
it does **not** blindly repeat an uncertain apply.

These components are internal and are not connected to a public mutation API
or to the incident workflow until exact-plan human approval (P2) is implemented.
A simulator effect is **not** a real recovery. No infrastructure mutation
connector is enabled or claimed conformant.

## B4 admission and queue health

Admission now increments a durable, tenant-scoped fixed-minute counter in the
same transaction as incident + outbox. Excess requests return HTTP 429 and
Retry-After. Duplicate idempotency replays do not consume additional capacity.
The `GET /v1/operations/queues` endpoint shows tenant-scoped pending outbox
and active-incident counts. `GET /v1/health/ready` checks database readiness.
Run `python -m backend.workers.state_age` separately to log phase-age alerts.
This is a baseline quota; worker-concurrency and model token budgets are not yet
implemented and require measured tuning.

## A1/A2/A3/A4 safe investigator baseline

An incident worker now runs a bounded, deterministic simulator investigation:
probe health, then request a version observation if the target appears unhealthy.
An allowlisted gateway verifies the tenant, incident and target, decrements a
durable per-incident probe budget and records immutable evidence IDs, hashes,
source version and timestamps. Probe attempts are idempotent by incident/step.
The read-only API offers evidence, hypothesis steps, and effect receipt views.
A4 conservatively abstains when the simulator target is absent or evidence is
insufficient. This is **not** a trained LLM or empirically calibrated abstention
policy; those require a provider, model evaluation fixtures, and held-out data.

## Local plan decision and simulated recovery

Create a single isolated simulator fixture after migrating the database:

```bash
python -m backend.workers.seed_simulator --tenant demo --target checkout
```

Start the API, dispatcher, Temporal worker, and React development server as
documented above. Admit an incident for `checkout`, then refresh its status
until the read-only investigator creates a simulator plan and reaches
`AWAITING_APPROVAL`. Use `GET /v1/incidents/{id}/plans` to inspect its
action, evidence IDs, digest, target version and plan version.

POST `/v1/plans/{plan_id}/decisions` with
`{"decision":"APPROVE"}` or `{"decision":"REJECT"}` and the current
numeric `If-Match` plan version. The decision and outbox signal commit
atomically. On approval the Temporal worker rechecks the plan and applies only
to the local simulator, with fence/version CAS and an effect receipt. It
projects VERIFIED only after independent simulator observation. The dev token
is not production reviewer authentication. Never connect a real adapter
without a separate role/approval and conformance gate.

## Native integration verification

GitHub Actions runs a PostgreSQL + Temporal integration job without Docker:
install native PostgreSQL, apply all Alembic migrations, start the local
Temporal test server, then run the actual FastAPI/dispatcher/worker approval
flow against a local simulator target. The job checks evidence, receipt,
target version and final workflow projection, then downgrades migrations.
This proves the **isolated lab path**, not multi-host resilience or
production-target safety. Integration tests are separated from fast unit
tests by pytest markers.
