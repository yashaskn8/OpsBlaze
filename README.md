# OpsBlaze

OpsBlaze is a **local, simulator-only incident investigation and controlled recovery lab** based on the TITAN-20 engineering blueprint (formerly RehearsalGate).

**Status:** a Docker-free end-to-end simulator path is implemented and tested with FastAPI, PostgreSQL, Temporal, and React/TypeScript. **It is not production-ready**: real-target adapters, externally verified production identity configuration, independent approvals, held-out evaluations, calibrated AI, and policy promotion remain incomplete.

See [implementation status](docs/implementation-status.md) for all 20 TITAN-20 capability areas, test evidence, and missing gates. Coding guidelines are in [AGENTS.md](AGENTS.md).

### Forensic paging and redacted privacy export

For a tenant-authorized incident, `GET /v1/incidents/{id}/lineage` returns
bounded durable nodes, typed causal links, explicit missing references and
`truncated_sources`. If any source is truncated, the React workbench offers
additional read-only records through `GET /v1/incidents/{id}/lineage/records?kind=hypothesis&cursor=<uuid>&limit=50`.
Pages contain only UUID, type/status metadata, independently checked
causal links, and explicit missing or out-of-scope reference markers.
Relationships are capped at 200 references per page with an explicit
truncation warning. The explorer does **not** invent missing OpenTelemetry
spans, claim that a plan digest alone authorizes an effect, or certify
a complete cross-service trace.

Operators can export their tenant's **metadata-only** privacy ledger from
`GET /v1/privacy/export?kind=consents&limit=50` or
`?kind=capsules&cursor=<uuid>&limit=50`. The result includes durable
consent and revoked/expired capsule identifiers and status, but no target
digest, raw probe evidence, token, secret or prompt. Operators can
also use `POST /v1/experiences/{id}/quarantine` with the capsule's
`consent_epoch` in `If-Match` to invalidate suspect memory and all derived
lessons. This is **not** a full personal-data access/export or cross-store/
backup erasure certification.


### Consented experience pages (E1)

`GET /v1/experiences?limit=50&cursor=<uuid>` returns a bounded
`{records, next_cursor, redacted}` response for current tenant-owned
capsules only when consent remains allowed, the epoch matches, the source
outcome remains verified, the capsule has not expired and its target digest
still matches the originating incident. Use `next_cursor` until it becomes
`null`; an empty page can still have a next cursor if invalid stored rows
were rejected. This is a redacted source-reference view, not a raw transcript.
Previous unpaged array clients must switch to `records`.

### Tenant-wide memory consent and automatic projection (E1)

Independent production `security_admin` credentials can read
`GET /v1/privacy/experience-consent` and version-check updates through
`POST /v1/privacy/experience-consent` (JSON `{"allow":false}`,
`If-Match: <current epoch>`; returns `204`). A tenant with no policy
retains the existing explicit **per-incident opt-in** behavior until its
first override. Changing a tenant epoch permanently invalidates older
capsules and lessons, including when consent is later re-enabled. A
background `python -m backend.workers.maintenance` run scrubs the
superseded capsules and lessons in up to 500-row batches. Retrieval is
denied **before** the sweep; a backup erasure certificate is not implied.

A verified E2 outcome now commits a `PROJECT_EXPERIENCE` outbox
intent in the same database transaction. The dispatcher handles that
local intent (never a real effect), pins it to the exact outcome revision,
and either captures an eligible capsule idempotently, skips revoked/
superseded sources, or retains the intent for retry after a storage
failure. Replaying after a lost outbox ACK cannot duplicate a capsule.
This does not yet provide a dedicated least-privilege projection process.

### Durable queue health (B4)

The workbench's **Queue health** panel calls `GET /v1/operations/queues`
using the authenticated tenant's credentials. It reports durable pending
outbox and active incident counts, exact nearest-rank p95 pending age,
oldest pending/active phase ages, delivery attempt pressure, and aged
unresolved `SENT`, `UNKNOWN`, or `CONFIRMED` receipts. Results contain
only aggregate values, not incident targets or secrets. Aged work is an
operator alert, **not** an automatic mutation permission.

The native `backend.workers.state_age` monitor retries database faults
and reports exact aged totals while limiting per-record log details.
Admission returns `429` for capacity or rate limits and `503` with a
`Retry-After` header when durable persistence is unavailable; it never
reports a phantom `202` acceptance on that path. This lab does not
establish throughput, multi-host scheduling latency, or production SLOs.

### E1 projection dead-letter review

Only local `PROJECT_EXPERIENCE` intents have a bounded failure policy.
A malformed immutable payload is immediately marked `INVALID_PAYLOAD`;
other projector failures retry with the existing durable backoff and become
`RETRY_EXHAUSTED` after five failed claims. The terminal record is **not**
marked delivered, is no longer claimable and cannot block later incident
signals. Temporal start/plan/cancel signals retain their existing retries.

`GET /v1/operations/dead-letters?cursor=0&limit=50` lists only this
tenant's redacted entry ID, source incident ID, attempts, fixed code and
review status. Operators can send
`POST /v1/operations/dead-letters/{id}/acknowledge` with `If-Match`
set to the attempts count. The operation is one-winner, recorded with an
actor pseudonym, and never redrives the entry or changes live effects.
The React workbench shows the review list and an unresolved count on the
Queue health panel. Dead-lettered items are excluded from pending queue age.

**An acknowledged failure remains terminal.** Fixing and replaying a
dead-lettered projection is not yet an authorized capability; do not
manually edit outbox rows to bypass review. Dead-letter diagnostics never
persist raw exceptions, target IDs, payloads or secrets.

### Redacted diagnostic traces and synthetic capacity evidence

OpsBlaze emits allowlisted OpenTelemetry spans from durable admission,
outbox delivery, workflow status projection, independent recovery
observation, and experience capture. Only `incident_id`, durable outbox
entry ID, and a fixed component label are attached; **never** raw target
IDs, tokens, user identifiers, excerpts, prompts or exception text.
Tracing exports nothing by default. To configure your own collector:

```bash
python -m pip install -e ".[observability]"
# Set OPSBLAZE_OTEL_TRACES_ENDPOINT=https://your-trusted-collector/v1/traces
# Set OPSBLAZE_OTEL_SAMPLE_RATIO=0.1
```

Production endpoints require HTTPS without embedded URL credentials or
query tokens; development permits only loopback HTTP. Independently
deployed collectors, W3C propagation through Temporal, sampling decisions,
retention, and end-to-end span links are **not yet certified**.

CI also runs a Docker-free **in-process only** latency profile against a
fresh synthetic SQLite database using FastAPI's ASGI transport. Its raw
p50/p95/p99 samples are uploaded as the `synthetic-local-capacity` Actions
artifact. To reproduce without external network traffic:

```bash
python -m backend.benchmarks.local_capacity --output ci-artifacts/local-capacity.json
```

These numbers are not PostgreSQL, Temporal, multi-host, or production SLO
measurements. Real workload capacity and failover campaigns remain open.

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

The local React review card now shows the separate grant state,
reviewer pseudonym, exact action scope, reason and expiry. It collects
an explicit decision reason and provides a version-bound revocation
button while the grant remains unconsumed. It never treats a button
click or SSE event as authorization; server-side checks control execution.

## Durable worker readiness

The dispatcher and incident-worker processes now maintain one durable
heartbeat row per required role, updated every ten seconds in native
PostgreSQL. When `OPSBLAZE_ENVIRONMENT=production`,
`GET /v1/health/ready` returns 503 unless the database is reachable
and both roles have fresh heartbeats (default stale threshold: 30s).
Configure `OPSBLAZE_WORKER_HEARTBEAT_SECONDS` and
`OPSBLAZE_WORKER_STALE_SECONDS` to match observed load.

Heartbeats demonstrate process/database connectivity only; they do not
prove Temporal task-processing progress, third-party availability,
redundancy or disaster recovery. These remain separate operational gates.

## SSE notification retention and cursor gaps

Run `python -m backend.workers.event_retention --days 7` from an
authorized maintenance environment. The one-shot worker purges at most
500 expired UI notifications per transaction and atomically advances
per-incident `last_purged_id` watermarks. A stale REST/SSE cursor receives
410 with the minimum safe continuation ID; an active stream emits a
lightweight `gap` event if expiry happens while connected. The React
client discards its stale timeline, refetches the authoritative incident
snapshot and resumes from the watermark. Incident, approval, effect and
outcome audit rows are never deleted by this job.

The native Temporal integration suite also exercises worker restart:
it stops the first worker at `AWAITING_APPROVAL`, signals the running
workflow while no worker is connected, then resumes with a new worker.
It asserts that previously completed probes do not repeat and the
simulator execution activity runs exactly once in this scenario.
It does not replace multi-host, load, corruption or disaster-recovery tests.

For B1 anti-entropy diagnosis, run
`python -m backend.workers.reconciler --stale-seconds 300` with the
native Temporal service and PostgreSQL available. It checks a bounded
set of aged active projections whose workflow-start intent was
acknowledged, then reports the actual Temporal status or
`UNKNOWN` without changing incident state. A human approval wait
is excluded from the stuck-worker classification. This is a
read-only diagnostic, not automatic recovery or failover.

## Browser sign-in (OAuth2/OIDC authorization code with PKCE)

The React UI has two explicitly separated authentication modes. Without
Vite OIDC configuration it remains the existing local-development token
workbench. For a configured public SPA client, set these **public** build
variables (never an OAuth client secret):

```env
VITE_OIDC_AUTHORITY=https://idp.example.com/realms/ops
VITE_OIDC_CLIENT_ID=opsblaze-spa
VITE_OIDC_SCOPE=openid profile opsblaze-api
```

Register `https://your-opsblaze-origin/auth/callback` (or
`http://127.0.0.1:5173/auth/callback` for a local SPA test) as the
redirect URI at the IdP. The authority itself must be HTTPS. Enable
authorization-code + PKCE, configure the SPA's token endpoint CORS policy,
and issue RS256 **JWT access tokens** with the pinned API audience,
`tenant_id`, `sub`, and `roles` claims required by the backend.
The frontend sends bearer tokens only in authorization headers and
fetches `GET /v1/auth/me` to learn server-verified tenant and roles.
No raw identity-provider password or token is placed in URLs or
localStorage; redirect state temporarily uses sessionStorage and access
tokens stay in memory. Refreshing a signed-in page requires sign-in
again. No silent renewal or long-lived refresh token is enabled.
The "Clear browser session" control clears only this app's memory;
it is not IdP-wide logout. Production must use HTTPS, a same-origin API,
appropriate CSP and a trusted configured IdP. Browser E2E and actual
IdP integration remain unverified release gates.


## Aggregate counter retention

`python -m backend.workers.budget_retention` prunes fixed-minute
admission-rate and optional model-token budget counters older than
`OPSBLAZE_BUDGET_COUNTER_RETENTION_DAYS` (default seven days).
Each transaction removes at most 500 rows per table and is safe to
repeat. Active windows remain intact. The command deliberately
does not remove any incident, approval, effect, outcome or experience
audit records. Provider billing and historical cost reporting require
a separate immutable metering system.

Production `GET /v1/health/ready` additionally performs a bounded
live Temporal gRPC health check after releasing its PostgreSQL
transaction. An unreachable workflow server returns 503 even if
stale workers still have recent heartbeat records. Temporal liveness
does not prove that individual workflows are executing correctly;
use the read-only anti-entropy scan and end-to-end recovery tests.


## Temporal transport security

All Temporal API, worker and diagnostic processes use a common connection
helper. `OPSBLAZE_TEMPORAL_TLS_ENABLED=true` is mandatory for production;
otherwise startup and readiness fail closed. Optional
`OPSBLAZE_TEMPORAL_API_KEY` must come from an untracked secret manager.
Development's native local Temporal server remains compatible with
plaintext. Cloud and private-CA/mTLS configurations still require actual
external integration testing.

## Native scheduled maintenance

A Docker-free systemd timer example is in
[deploy/systemd](deploy/systemd/README.md). It calls
`python -m backend.workers.maintenance` hourly to scrub expired
experience capsules, purge old SSE notifications with cursor watermarks,
and prune obsolete rate/token counters. Each category commits
independently, and work per invocation is explicitly bounded.


Production readiness additionally requires complete, trusted OIDC
issuer/audience/JWKS configuration. Missing identity-provider settings
cause `/v1/health/ready` to return 503 even when PostgreSQL and
Temporal are reachable, avoiding a false-ready deployment.

The frontend now commits the runner-resolved npm v3 lockfile. CI uses
`npm ci --no-audit --no-fund` to verify that
`frontend/package-lock.json` matches `frontend/package.json`.
Local installs can use `npm ci` for the same dependency tree.

The operator workbench keeps OIDC access tokens in memory and verifies
the signed principal using `GET /v1/auth/me` before enabling a session.
Viewers may refresh incident evidence without acquiring the `operator`
or `reviewer` role; all mutating controls remain role-gated at the API.

The native PostgreSQL integration suite also races plan revocation
against the `PREPARED → SENT` effect checkpoint. Exactly one operation
may win: either the grant is consumed with the receipt in `SENT`,
or revocation persists and the receipt stays `PREPARED`. The test
isolates approval locking; separate tests check evidence validity.

Production-mode simulator gateway calls now fail closed if they attempt
to enter the `SENT` checkpoint without an explicitly identified approved
plan. This protects both the high-level execute helper and the lower-level
receipt state transition; the direct permit-only path remains available
solely in the isolated development simulator test harness.

## Production identity revocation boundary

A JWT can remain cryptographically valid after a human loses reviewer
authority. Production mode now checks a tenant-scoped database deny-list
on each authenticated request. A bearer token bearing only the
`security_admin` role may call
`POST /v1/security/identities/{actor_id}/revoke` with an audit reason.
No local development token can do this. The actor ID is a hashed,
non-reversible IdP subject identifier from `GET /v1/auth/me`, not a
name or email. Revocation is persistent and currently cannot be
undone from the API.

The security transaction invalidates that actor's pending approvals;
the B3 effect gateway additionally checks this deny-list before
`PREPARED → SENT`. PostgreSQL advisory serialization prevents a
reviewer grant from being created concurrently with its identity's
revocation. Effects already consumed or committed remain visible for
independent receipt reconciliation; revocation never fabricates a
rollback. The identity provider still needs a trusted administrative
workflow to publish role-removal events promptly; no external IdP
integration or operational security certification is implied.

## Operator-triggered B3 status reconciliation

A tenant-scoped operator may call `POST /v1/effects/{effect_id}/reconcile`
to query the original simulator effect identity and independently observe
its postcondition. This **never calls apply** and cannot create a new
effect. Cross-tenant IDs return 404; unsent PREPARED receipts and
unsupported adapters return 409 for manual review. VERIFIED receipts
are idempotently returned. This safe lab operation is not certification
for a real external adapter's status-lookup contract.

## Per-process worker backpressure

The Temporal incident worker has explicit validated slot limits:
`OPSBLAZE_WORKER_WORKFLOW_SLOTS=8` (2–64) and
`OPSBLAZE_WORKER_ACTIVITY_SLOTS=8` (1–64). These bounds prevent a
single worker process from accepting an unbounded number of concurrent
activities. Admission limits and durable token budgets remain separate.
Measure schedule-to-start latency and database contention before raising
these values; multi-host capacity and fair-share quotas remain unverified.

The native Temporal tests also verify that an approval signal received while
workers are stopped is immutable: a later contradictory signal cannot replace
its plan ID or convert a reviewer rejection into an approval. The API and
gateway still enforce independent plan versions and receipt permissions.


## Signed identity-provider revocation events (integration contract)

For production, configure an **independent** `OPSBLAZE_IDP_EVENT_AUDIENCE`
in addition to the existing pinned issuer/audience/JWKS settings.
`POST /v1/security/idp-events` accepts a short-lived, RS256-signed
IdP JWT through `Authorization: Bearer`. Its `aud` must be that
dedicated event audience (never the API audience); required claims are
`iss`, `sub`, `tenant_id`, `iat`, `exp`, `jti`, and
`event_type="IDENTITY_ACCESS_REVOKED"`. The issuer and signing key
are verified against the pinned production JWKS, and the validity
window is capped at five minutes. JSON bodies and browser/local tokens
cannot authenticate this endpoint.

One event atomically records a hashed replay ID, revokes the tenant's
pseudonymous identity and invalidates pending approval grants. Duplicate
deliveries are idempotent; conflicting reuses fail closed. No raw subject,
email, JWT or signing key is retained. This is intentionally a **full
access revocation**, not role-granular removal; unrevocation is not
available from the API. The IdP must be configured to emit this exact
event profile, and a real event delivery/key rotation/role-removal
campaign is required before calling the integration production-tested.
The endpoint is disabled without separate audience configuration.

## Protected offline evaluation (E5)

`python -m backend.evaluation.heldout --manifest manifest.json
--oracle protected-oracle.json --trials paired-trials.json` runs an
offline report on independently supplied label data. The manifest must
commit to the full canonical JSON SHA-256 of the oracle, a minimum of
32 complete pairs and the disjoint training-family list. The trials
file contains one candidate and one incumbent decision/cost/latency
observation for every case. The runner rejects duplicate/missing pairs,
training-family overlap, oracle changes, unbounded costs and invalid
negative controls. It returns aggregate safety, p95 latency, cost and
conservative paired-correctness intervals without echoing hidden
labels. The `blinded_cases` helper exposes only case IDs and family
names to a separately sandboxed candidate runner.

An `OFFLINE_PASS` is **not** a certified recovery benefit, a real
held-out experiment or release approval. Independent oracle custody,
actual incident outcomes, preregistration/signing, evaluator authority
and model-vs-runbook trials are still missing. The tool always reports
`promotion_eligible=false`; E6 exposure remains disabled.

## Worker failover rehearsal

The native PostgreSQL/Temporal integration suite tests concurrent
outbox dispatch against a single tenant, stops the first worker at the
human approval boundary, delivers an approval while no worker is
running, then starts a replacement worker. It checks one effect
receipt, one simulator application and the final target version.
This is an **independent-worker handoff on one CI host**, not a
multi-host/region failure campaign or measured availability result.


## E6 shadow rehearsal (still no policy promotion)

`POST /v1/rollouts/{id}/shadow/{incident_id}` records a deterministic,
tenant-scoped **would-assign** decision for an existing incident. It requires
operator authentication, an untampered draft bundle and an incident pinned to
the known-good incumbent; the rollout row is locked so pause/rollback cannot
interleave a new shadow observation. Duplicate calls reuse the same immutable
record. `GET /v1/rollouts/{id}/shadow-summary` reports only aggregate
hypothetical membership; it cannot claim outcome or recovery performance.
Every shadow response explicitly reports `exposure_enabled=false`.
The `/approve` gate remains closed until independent E5, signing, separate
human release authority and real canary fail-safe evidence exist.

## Expanded E8 adapter conformance

The development-only conformance suite now requires seventeen scenarios,
including missing effect-status retention, status recovery, and external
target-version changes. It pins the current simulator source digest and suite
version. A green `LAB_PASS` applies **only** to the disposable simulator:
it is not proof that a third-party adapter has stable idempotency keys,
atomic fences, independent postconditions or sufficient retention.
No external effect connector is enabled.
