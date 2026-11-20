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

## E2 observed outcome ledger

Simulator effects now require a separate postcondition witness before the
workflow can return VERIFIED. The witness is persisted as an append-only outcome
revision. A development operator can append a DISPUTED correction via
POST /v1/outcomes/{id}/feedback with a version-bound If-Match; automatic
reconciliation never silently overwrites that human dispute. GET
/v1/incidents/{id}/outcomes exposes the revision chain.

This witness is independent of the investigator's decision but still relies on
the local simulator, not an external production-grade oracle.

## E1 opt-in experience capture

Experience capture is disabled by default. POST
/v1/incidents/{id}/consent with an If-Match consent epoch (initially 0)
and {"allow":true} opts in; {"allow":false} revokes with a new epoch
and tombstones earlier capsules. Capture requires the latest recorded outcome
to be independently VERIFIED and undisputed. It stores only IDs, a salted-by-
tenant target digest, lineage and a bounded expiry, never raw logs/prompts.
GET /v1/experiences checks current consent and expiry at read time.

This is a laboratory privacy baseline; a production system needs reviewed
classification, retention, cross-store revocation propagation and a consent UI.

## E3 failure classification and improvement queue

The E3 baseline classifies E2 outcome revisions using typed facts (not a
model-authored postmortem). Disputed outcomes remain UNCERTAIN and cannot
seed an evaluation-ready candidate without a reviewed classification.
Failure signatures are tenant-scoped. Improvement drafts accept only fixed
safe change classes: read-only simulator probe order, bounded context limit,
or deterministic routing; SQL, arbitrary code, and permission expansion are
rejected. Submission uses a version check and never deploys the change.

The draft and submit endpoints currently use a development-only token;
production reviewer identity and auditable classification corrections are
not implemented. E5 offline evaluations must be built before rollout.

## E4 lesson registry

Lessons can be drafted only from currently consented, VERIFIED E1/E2
experience capsules. They have fixed typed rule IDs rather than free-form
instructions. Validation rechecks the latest outcome, consent epoch, expiry,
and target scope. Revocation is version-bound and retrieval checks all
source constraints again. The activation gate is deliberately **closed**:
validated lessons are not served as ACTIVE until E5 paired evaluation and
release review are implemented. Stored lessons do not grant tools or
permissions.

## E5 fixed-fixture paired smoke evaluation

An EVALUATION_READY constrained E3 candidate can freeze an immutable
candidate/incumbent/fixture digest with POST /v1/evaluations. POST
/v1/evaluations/{id}/run executes paired deterministic simulator decision
fixtures, including disputed outcomes, stale target versions and poisoned
lesson negative controls. GET /v1/evaluations/{id} returns both arms,
oracle-correctness, probe-count cost units and three hard non-regression
gates. The run is reproducible and idempotent.

**This is a smoke gate, not the blueprint's full independent held-out
experimental evaluation.** Results are SMOKE_PASS/SMOKE_FAIL and explicitly
promotion_eligible=false. It cannot authorize deployment, lesson activation,
or real-target effects. Full E5 needs an independently managed hidden-label
fixture store, measured paired-effect outcomes, uncertainty intervals, and
separate approval authority.

## P3 forensic trace graph

The operator workbench can load a bounded lineage view using
GET /v1/incidents/{id}/lineage. The server correlates incident, evidence,
hypothesis, exact plan, effect, independent outcome, experience, failure case,
and validated lesson records by their persisted IDs. Missing references are
reported explicitly, and absent observability spans are never inferred.
All reads are tenant-scoped and the local viewer shows status per record.
This is a durable-record projection, not a complete OTel span integration.

## E7 pinned lab routing

The Temporal workflow now runs a deterministic strategy selector before
investigation. If its tenant-scoped simulator fixture is unavailable or the
probe budget is insufficient, it permanently records HANDOFF and projects
NEEDS_REVIEW. Otherwise it records RUNBOOK with an explicit two-probe cap.
The decision is stable under activity replay and visible at
GET /v1/incidents/{id}/route, including its policy ID, reason and
non-random selection propensity. There is no autonomous model routing,
online bandit, or memory-aided live execution.

## E8 isolated adapter conformance smoke

Run `python -m backend.adapters.conformance` after migration to create
disposable simulator fixtures and record a code-digest-pinned conformance
report. The suite checks duplicate apply, status lookup, stale fence,
version CAS, lost acknowledgment, expired lease, and missing effects.
GitHub Actions runs the suite on native PostgreSQL after the end-to-end
test. GET /v1/adapters/simulator/conformance displays the latest local
report. The lab fixture data is deleted, leaving only the report.

**LAB_PASS is not external-adapter certification or production enablement.**
No production adapter registry, release-owner approval, or long-horizon
retention validation is present. Only the isolated simulator can mutate.

## E6 immutable policy drafts and disabled rollout previews

A candidate with an E5 SMOKE_PASS report can be frozen into a
content-digested policy DRAFT (POST /v1/policies); its tool catalog remains
the fixed read-only simulator catalog and model_id=none. Rollout DRAFT
records can preview deterministic 1–5% cohorts but are never referenced
by B1 admission or E7 routing. Pause and rollback transitions use If-Match.
Approval always returns 409 because a smoke run is insufficient.

No live canary, signed bundle attestation, true evaluation PASS, independent
release identity, or policy exposure is enabled. This prevents unreviewed
self-modification and makes incomplete gates explicit.

## P1 authoritative snapshots and durable events

GET /v1/incidents returns up to 50 recent tenant incidents.
GET /v1/incidents/{id} exposes its current version as an ETag.
GET /v1/incidents/{id}/events?after_id=N returns sequenced
lightweight status-change notifications with a continuation ID. Admission,
cancellation, plan decision and Temporal projection commit their event in
the same database transaction. Notifications never authorize an action:
the client must refetch the snapshot and send a version-bound mutation.

The event endpoint is bounded polling JSON, not yet SSE. Frontend
reconnect/gap recovery, a complete role-aware approval cockpit and load
testing remain for P1 production readiness.

The React workbench now offers recent tenant incident selection, a bounded
durable-event timeline, and four-second event polling that refetches the
authoritative versioned snapshot after changes. Controls are disabled if
the last event poll failed or has not synchronized. The API remains the
source of authorization; the UI never trusts event notifications as approval.

## S1 time-sealed paired simulator replay

POST /v1/experiments accepts one of three bounded laboratory fixtures
and a deterministic seed. It persists two isolated simulator arms:
RUNBOOK and ADAPTIVE_BASELINE, with identical initial state,
allowlisted probes, the same two-call budget and virtual-time evidence
release. The independent oracle checks target state rather than trusting
an effect acknowledgment. GET /v1/experiments/{id}/results returns
the per-arm traces, virtual time and oracle outcomes.

This is an isolated **deterministic baseline**, not a real LLM run,
statistically powered paired trial, hidden-label evaluation, or external
service workload. There is no Docker and no connection to live targets.

## Dispatcher ordering invariant

The outbox dispatcher only claims a CANCEL_WORKFLOW, review acknowledgment,
or plan-decision signal after the matching START_WORKFLOW intent has been
acknowledged as delivered. This prevents concurrent dispatchers from
signaling a workflow that another dispatcher has not finished starting.
Lost acknowledgments remain recoverable through a stable Temporal
workflow ID and replay of the pending start intent.

## Optional A1 structured read-only AI advisory

The default investigator remains deterministic. For an experimental
development-only model suggestion, install the optional SDK extra with
`pip install -e ".[dev,ai]"`, set `OPSBLAZE_AI_ADVISORY_ENABLED=true`
and provide `OPENAI_API_KEY` in your local untracked environment.
The only allowed model output is a typed suggestion to read
`simulator.version` or abstain after an unhealthy simulator observation.
No target IDs, raw logs, credentials, tool definitions or mutation
capabilities are sent to the provider. The API request sets
`store=False` and caps generated output.

An incident reserves at most one model call in PostgreSQL before network I/O;
a crash, invalid result, refusal or outage falls back to deterministic
investigation and never retries the ambiguous request blindly. The
model cannot select a real adapter or authorize an effect. This optional
path is not a calibrated production agent, and CI tests use fake clients
rather than paid model calls.

## E3 fact-bound reviewer corrections

A development reviewer can correct a failure case at
POST /v1/failures/{id}/review using the current classification version.
The server requires a matching denied probe or failed/unknown effect receipt,
records a separate immutable review revision, moves the case to the new
typed signature cluster and quarantines proposals/evaluations from the
previous signature. It cannot invent a root cause from persuasive text.
Real reviewer identities and independent review policy remain required
before production use.

## Simulator conformance execution gate

Before starting a new approved simulator effect, the action gateway now
requires a passing conformance report for the **currently installed simulator
code digest**, from the current suite version, within seven days. On missing,
stale, failed or mismatched evidence it returns NEEDS_REVIEW without
applying a new effect. Existing receipt reconciliation remains available
even if a previously approved adapter version becomes disabled.
Run `python -m backend.adapters.conformance` after changing simulator
code and before using the local approval flow. This is a laboratory
safety gate, not external production certification.

## A4 typed decision assessments

Before a simulator plan is proposed, the investigator records a
versioned DecisionAssessment. Hard gates verify both allowed evidence
sources, tenant/incident scope, evidence hashes, recent observation times,
target-version consistency, and any denied probe or exhausted budget.
Failures are explicit ABSTAINED reasons rather than invented confidence.
GET /v1/incidents/{id}/assessments returns the decision lineage.

The lab risk_score is a deterministic 0/100 guardrail indicator,
**not an empirically calibrated probability**. A4 calibration
requires independent development and held-out data before production use.
