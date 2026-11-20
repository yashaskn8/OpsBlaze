# TITAN-20 implementation status

This matrix distinguishes **implemented local code** from the full blueprint's production guarantees. A feature's existence is not evidence of production readiness. All paths that could affect real infrastructure are deliberately disabled. See the project's blueprint PDF for the full contracts.

| Feature | Implemented simulator/lab baseline | Important missing validation or capability |
|---|---|---|
| B1 — Durable incident workflow | FastAPI + atomic PostgreSQL admission/outbox, stable Temporal ID, separate dispatcher/worker, cancellation and review signals | Multi-host failover/replay campaigns, old-history compatibility, production HA and runbooks |
| B2 — Resource reservation | Leased target reservations, monotonic fencing, version checks | Real adapter-enforced fencing and failover/clock tests |
| B3 — Effect receipt gateway | Durable effect states, idempotent local adapter, unknown-outcome reconciliation | External effect acknowledgment/retention guarantees |
| B4 — Budgets and backpressure | Durable per-tenant fixed-minute admission quota and age/queue monitoring | Workload-specific concurrency limits, external model budgets, load characterization |
| A1 — Adaptive investigation | Deterministic simulator probe loop; optional typed read-only model advisory | Evaluated multi-probe model policy, empirical quality and token metrics |
| A2 — Tool policy gateway | Read-only probe catalog, tenant scope, durable per-incident probe budget | Production role-based policies, external connector permissions |
| A3 — Evidence provenance | Evidence IDs, timestamps, source versions and SHA-256, hypothesis steps | Signed external telemetry attestation, robust retention/redaction review |
| A4 — Safe abstention | Typed risk assessment, evidence integrity/freshness/target-version checks, fail-closed abstention | Empirical calibration with independent held-out families; current 0/100 is **not a probability** |
| P1 — Operator workbench | React incident list, plan actions, ETag/snapshot, durable sequenced event polling | Production RBAC, reconnect/load testing, accessibility audit and native SSE |
| P2 — Approval boundary | Exact-plan digest and version-bound local decision, revalidation before simulator effects | Real authenticated separate approver role and two-person policy |
| P3 — Forensic graph | Evidence→hypothesis→plan→effect→outcome lineage, explicitly missing refs | Comprehensive OpenTelemetry span linking and performance checks |
| S1 — Paired replay | Isolated, time-sealed deterministic twin fixtures with an independent postcondition | Actual paired model-vs-runbook controlled trial, statistical inference |
| E1 — Experience capture | Consent-scoped metadata capsules with revocation/TTL and read-time outcome-revision checks | Cross-store deletion and approved classification/retention controls |
| E2 — Outcome adjudication | Separate simulator witness, append-only outcomes and disputes | Independent real-world recovery oracle and delayed outcome reconciliation |
| E3 — Failure learning | Fact-backed taxonomy, reviewer corrections, allowlisted change candidates | Independent reviewers, causal attribution beyond typed simulator facts |
| E4 — Validated memory | Typed lesson drafts, source/consent checks, validation/revoke | Release gate for activation, measured retrieval effect and revocation propagation |
| E5 — Evaluation | Frozen candidate/incumbent manifest, deterministic paired fixtures and hard smoke gates | Hidden-label evaluation store, measured effect outcomes, confidence bounds, independent evaluator |
| E6 — Policy rollout | Content-digested drafts, admission-time incumbent pinning, deterministic cohort preview, pause/rollback records | **Live rollout disabled** pending real E5 PASS, signed bundle and release authorization |
| E7 — Strategy routing | Pinned tenant-scoped RUNBOOK/HANDOFF choice, deterministic cost cap | Evaluated contextual policies, measured route accuracy, memory/agent variants |
| E8 — Adapter conformance | Eight disposable simulator checks, code-digest pinning and expiry gate | External adapter conformance/fault-injection/retention campaign |

## Current demonstrated checks

GitHub Actions CI defines four jobs: `test` (SQLite unit/API checks), `frontend` (React/TypeScript build), `temporal-smoke` (native local Temporal replay/signal smoke), and `postgres-temporal-e2e` (native PostgreSQL migrations, disposable conformance, end-to-end simulator incident/recovery, migration downgrade). The build is Docker-free.

What CI **does not** prove: no full production incident recovery, no real external adapter, no load/HA numbers, no trusted production user authentication, no empirical risk calibration and no promoted AI policy. Never report a smoke PASS as deployment approval.

## Conditions to revisit before production

Implement real tenant identity, independently scoped operator/reviewer/release roles, complete audit/secret controls, adapter conformance for each external target, full B1 failure/replay testing, realistic workload budgets, independent held-out E5 measurements, and a signed human-controlled E6 rollout. Live infrastructure effects and learned-policy activation must remain disabled until those safeguards are validated.
