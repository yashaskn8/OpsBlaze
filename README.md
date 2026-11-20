# OpsBlaze

OpsBlaze is a **local, simulator-only incident investigation and controlled recovery lab** based on the TITAN-20 engineering blueprint (formerly RehearsalGate).

**Status:** a Docker-free end-to-end simulator path is implemented and tested with FastAPI, PostgreSQL, Temporal, and React/TypeScript. **It is not production-ready**: real-target adapters, production identity and approval roles, held-out evaluations, calibrated AI, and policy promotion remain disabled or incomplete.

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

The local token is **not** reviewer authentication or a real multi-tenant security system. The API deliberately rejects tenant requests when `OPSBLAZE_ENVIRONMENT=production`. **Do not expose this application publicly or connect it to real infrastructure.**

## Verification

Fast unit tests, a native Temporal workflow smoke test, a native PostgreSQL/Temporal end-to-end test and a TypeScript production build run in GitHub Actions on `main` without Docker.

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

The authorized operator stream at `GET /v1/incidents/{id}/stream` accepts
`after_id` and `Last-Event-ID`, replays up to 100 durable status events per
tick, sends keepalives, and closes after a bounded lifetime. The browser
uses an authenticated fetch stream (credentials never enter query strings),
reconnects with its last event ID, and falls back to JSON polling. Event
payloads are advisory only; the UI always refreshes the authoritative
versioned snapshot before acting. The SSE path remains a development-only
interface until production identity and load/concurrency testing exist.

Every new incident also pins `simulator-lab-route-v1` in
`policy_assignment` within the atomic admission transaction. Existing
incidents are pinned by migration 0019. E7 verifies that assignment before
routing and safely hands off when the pin is missing or changed. The policy
view is `GET /v1/incidents/{id}/policy`. Experimental E6 rollout drafts
still cannot assign themselves to incidents or enable real effects.

Workflow status projection is now an explicit phase-predecessor state machine.
Terminal `VERIFIED` requires `EXECUTING`, and repeated Temporal activity
delivery does not increment the version or duplicate UI events. Cancellation
is accepted only before the execution checkpoint; it never replaces an
in-flight effect with an untrue `CANCELLED` projection.

B4 admission now additionally enforces `OPSBLAZE_MAX_ACTIVE_INCIDENTS_PER_TENANT`
and `OPSBLAZE_MAX_ACTIVE_INCIDENTS_GLOBAL` by recounting durable
nonterminal incidents in PostgreSQL, under an admission transaction
advisory lock. Crash recovery does not depend on an in-memory counter,
and idempotent retries do not consume another slot. The global lock is
a simple lab baseline; measure contention before expanding worker
capacity. Expired/abandoned work requires B1 recovery or operator review,
not silent capacity forgiveness.

Run `python -m backend.workers.retention` as a one-shot maintenance
command to scrub at most 500 expired E1 capsules per invocation. It removes
their target digests and evidence references and revokes linked lessons in
the same transaction. Repeated runs are idempotent and leave minimal audit
IDs; no raw incident logs are added. Production retention scheduling and
backup/restore purge verification remain separate operational gates.

The CI backend job also runs a narrow Ruff undefined-name check
(`F821,F822,F823`) before tests. This intentionally avoids an
unrelated repository-wide style rewrite.

E5 reports now expose paired correctness wins/losses/ties, candidate
and incumbent probe-cost units, and a deliberately conservative
Hoeffding interval for paired binary correctness differences. If a
frozen trial pair is missing, report integrity becomes `INCOMPLETE`
without granting promotion. These are six public deterministic smoke
fixtures, **not** held-out production results: verified recovery per
cost remains explicitly `null` and promotion stays disabled.

Optional A1 model calls also reserve a conservative 512-token budget
against the tenant's durable fixed-minute counter before contacting the
provider. The cap is set by `OPSBLAZE_AI_TOKEN_LIMIT_PER_MINUTE` (default
2048). `GET /v1/operations/model-budget` shows reserved and provider-
reported usage; a failed or exhausted reservation falls back to the
deterministic investigator. This bounds the number of optional calls,
but a provider's actual prompt accounting may differ from the local
reservation, so it is not a billing guarantee.

E3 reviewer corrections now invalidate downstream E5 evaluations and E6
policy/rollout drafts in the same transaction as the fact-backed taxonomy
revision. Stale classification evidence can no longer leave a policy draft
presented as valid.

E3 classification now rejects older outcome revisions at ingestion and
requires a current, classified source case both when drafting and when
submitting an improvement candidate for evaluation. A later outcome
revision therefore blocks stale candidates even if a separate invalidation
worker has not yet updated the cluster projection.

The P2 execution gate also checks the **meaning** of the pinned observations:
the initial health must have been false, the version observation must match
the exact pinned integer, and the target must still be unhealthy. Rehashing
an invented healthy observation does not make it authorization evidence.

B4 now records an immutable `outbox.created_at` separate from the
retry lease `available_at`. The state-age monitor includes every
active incident phase (including EXECUTING) and overdue undelivered
intents even when a retry has been scheduled into the future.
`GET /v1/operations/queues` reports the oldest pending intent age
for the authenticated local tenant. Monitoring remains read-only and
never marks unresolved work as successful.

The native PostgreSQL integration suite also races five different incidents
for one simulator resource. Only one may hold the active fence, and a
superseded permit cannot apply an effect after a newer epoch is reserved.
This checks the database lock behavior instead of relying on SQLite mocks.

### Separate local reviewer credential (P2)

To approve or reject simulator plans through HTTP, also configure
`OPSBLAZE_REVIEW_TOKEN` with a different value than
`OPSBLAZE_LOCAL_TOKEN`. Supply it in the UI's local reviewer field.
The plan decision endpoint checks the second secret and persists
`decided_by=DEV_REVIEWER_CREDENTIAL` with the existing exact-plan
version and evidence checks. The operator token alone cannot decide
plans. These are still local shared secrets, **not real user identity**
or production separation of duties: production mode remains closed.
