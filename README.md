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
