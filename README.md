# OpsBlaze

OpsBlaze is a **local, simulator-only incident investigation and controlled recovery lab** based on the TITAN-20 engineering blueprint (formerly RehearsalGate).

**Status:** a Docker-free end-to-end simulator path is implemented and tested with FastAPI, PostgreSQL, Temporal, and React/TypeScript. **It is not production-ready**: real-target adapters, externally verified production identity configuration, independent approvals, held-out evaluations, calibrated AI, and policy promotion remain incomplete.

See [implementation status](docs/implementation-status.md) for all 20 TITAN-20 capability areas, test evidence, and missing gates. Coding guidelines are in [AGENTS.md](AGENTS.md).

## Setup (no Docker)

Install Python 3.11+, native PostgreSQL and a native Temporal development server. Create a PostgreSQL database and credentials, then configure them in a local untracked `.env` copied from `.env.example`.

```bash
python -m venv .venv
# Activate .venv for your shell
python -m pip install -e ".[dev]"
alembic upgrade head
```

Start the Temporal development server in a separate terminal. Keep its data persistent for restart experiments. Then prepare an **isolated simulator fixture** and run the conformance suite:

```bash
python -m backend.workers.seed_simulator --tenant demo --target checkout
python -m backend.adapters.conformance
```

Run each long-lived process in a separate terminal:

```bash
uvicorn backend.api:app --reload
python -m backend.workers.dispatcher
python -m backend.workers.incident_worker
python -m backend.workers.state_age
```

Start the operator UI:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173` and supply the **local** API token from your `.env`. The API specification is at `http://127.0.0.1:8000/docs`.

## Run a local incident

1. Admit `checkout` from the UI or call `POST /v1/incidents` with headers `X-Tenant-Id: demo`, `X-OpsBlaze-Token`, and a stable `Idempotency-Key`, sending `{"target_id":"checkout"}`.
2. The database commits an incident and a workflow-start outbox event together. A separate dispatcher starts the stable Temporal workflow. The worker probes simulator health/version and records evidence and a bounded decision assessment.
3. Refresh the UI. At `AWAITING_APPROVAL`, examine the proposed exact simulator plan. Approval/rejection uses the plan's current numeric `If-Match` version. Acknowledgment and cancellation are separate flows.
4. If approved and all fences, evidence checks, and current adapter conformance pass, the worker applies **only** `SIMULATOR_MARK_HEALTHY`. It reconciles its effect receipt and requires an independent simulator postcondition observation before setting `VERIFIED`.
5. The UI can show evidence, effect receipts, trace lineage, recent incidents, sequenced SSE event notifications with a polling fallback, and isolated paired-replay results. These notifications never authorize actions; decisions rely on authoritative snapshots and server-side version checks.

The local token is **not** reviewer authentication or a real multi-tenant security system. The API requires signed, issuer- and audience-validated identity-provider bearer tokens when `OPSBLAZE_ENVIRONMENT=production`. **Do not expose this application publicly or connect it to real infrastructure.**

## Verification

Fast unit tests, native Temporal smoke and JSON-history replay tests, a native PostgreSQL/Temporal end-to-end test and a TypeScript production build run in GitHub Actions on `main` without Docker.

```bash
python -m pytest -q
cd frontend && npm run build
```

The broader integration suite needs native PostgreSQL/Temporal and the matching environment configuration. Inspect [.github/workflows/ci.yml](.github/workflows/ci.yml) for the exact CI commands. Passing simulator tests do not prove high availability or safe production recovery.

## Optional AI read-only advisory

By default investigation is deterministic. For an **optional development-only** advisory after an unhealthy simulator observation:

```bash
python -m pip install -e ".[dev,ai]"
# Set OPSBLAZE_AI_ADVISORY_ENABLED=true locally
# Supply OPENAI_API_KEY through a local untracked environment
```

The model may suggest only the allowlisted `simulator.version` probe or abstention. The server owns all probe permissions, evidence checks, plan approvals and actions. A failed or uncertain provider call falls back safely without blind retries. CI does not make paid provider calls.

## Design invariants

- PostgreSQL owns admission, approval state, effect receipts, and outbox events; Temporal owns workflow history. No external calls occur inside an incident/outbox transaction.
- Idempotency keys and every API read are tenant-scoped. Plan/cancel/review mutations use optimistic version checks.
- Only the disposable simulator adapter is present. Leases, monotonic fences, target-version CAS, effect identities, and no-blind-retry reconciliation remain mandatory.
- Safety evidence is required at **approval and execution**, not just at proposal creation. `NEEDS_REVIEW` is preferable to invented recovery.
- Experience capture requires consent; lessons recheck current lineage and remain inactive. E5 smoke evaluations **never** promote policy. Live rollout is deliberately closed.
- All development occurs directly on `main`, without branches, pull requests, or Docker.

## Implementation details

See [feature notes](docs/feature-notes.md) for the additional
SSE, policy, recovery, admission, retention, AI budget, and review
safeguards. The [TITAN-20 status matrix](docs/implementation-status.md)
identifies the remaining acceptance blockers. Use the latest passing
[GitHub Actions run](https://github.com/yashaskn8/OpsBlaze/actions)
to verify the exact code revision before deployment.

## Production identity boundary (configuration required)

In production mode, configure `OPSBLAZE_OIDC_ISSUER`,
`OPSBLAZE_OIDC_AUDIENCE`, and `OPSBLAZE_OIDC_JWKS_URL` using
your trusted provider's HTTPS endpoint (JWKS must share the issuer's
hostname). The API verifies the signed RS256 access token's issuer,
audience, expiry, issued-at, signature and key ID. Token claims include
`tenant_id`, `sub`, and a `roles` list containing only `viewer`,
`operator`, and/or `reviewer`. Admission and ordinary writes require
`operator`; reviewer-only decisions require `reviewer`. The server
derives tenant identity from the token, checks any supplied
`X-Tenant-Id` against it, and stores a non-reversible subject
pseudonym on incident admission. An incident's creator cannot approve
their own plan, even with both roles.

The development shared-secret headers **never** authenticate in
production. Missing configuration, invalid signatures and unavailable
signing keys fail closed. The React UI currently supports only local
development credentials; deployment must supply an OAuth2/OIDC login
client and HTTPS reverse proxy before humans can use this boundary.
This auth milestone does not authorize real infrastructure effects:
the simulator gateway and independent E5/E6 release blocks remain.

## Durable plan approvals (P2)

A plan review now records a separate tenant-scoped approval grant with
the exact plan digest, reviewer pseudonym, action scope, reason and expiry
(default 15 minutes, configurable up to one hour). The signed JWT verifies
a reviewer role at decision time. `GET /v1/plans/{id}` exposes a redacted
approval summary. `POST /v1/approvals/{id}/revoke` requires a reviewer
and `If-Match` version; it also invalidates the approved plan.

The simulator effect gateway rechecks the grant, evidence and policy epoch
inside its transaction before moving the receipt to `SENT`. That same
transaction consumes the grant, preventing a subsequent revocation
from falsely cancelling an already-started effect. Existing receipts
remain independently reconcilable. Legacy approved plans without grants
cannot initiate new effects. The real-adapter boundary remains disabled.
