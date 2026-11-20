# TITAN-20 implementation status

This matrix distinguishes **implemented local code** from the full blueprint's production guarantees. A feature's existence is not evidence of production readiness. All paths that could affect real infrastructure are deliberately disabled. See the project's blueprint PDF for the full contracts.

| Feature | Implemented simulator/lab baseline | Important missing validation or capability |
|---|---|---|
| B1 — Durable incident workflow | Atomic PostgreSQL admission/outbox, stable Temporal workflow IDs, signal ordering, status state machine, lost-ack, worker-restart and history replay tests, read-only anti-entropy scan | Multi-host failover/replay campaigns, old-history compatibility, multi-host HA and failover campaigns |
| B2 — Resource reservation | Leased target reservations, monotonic fencing, version checks | Real adapter-enforced fencing and failover/clock tests |
| B3 — Effect receipt gateway | Durable effect identities, exact-permit checks, unknown-outcome reconciliation, simulator postcondition | External effect acknowledgment/retention guarantees |
| B4 — Budgets and backpressure | Admission rate/active-work caps, durable AI budgets, outbox age, worker heartbeats and bounded counter retention | Workload-specific concurrency limits, external model budgets, load characterization |
| A1 — Adaptive investigation | Deterministic simulator probe loop; optional typed read-only model advisory | Evaluated multi-probe model policy, empirical quality and token metrics |
| A2 — Tool policy gateway | Read-only probe catalog, tenant scope, durable per-incident probe budget | Production role-based policies, external connector permissions |
| A3 — Evidence provenance | Evidence IDs, timestamps, source versions and SHA-256, hypothesis steps | Signed external telemetry attestation, robust retention/redaction review |
| A4 — Safe abstention | Typed risk assessment, evidence integrity/freshness/target-version checks, fail-closed abstention | Empirical calibration with independent held-out families; current 0/100 is **not a probability** |
| P1 — Operator workbench | React incident list and plan actions, ETag snapshots, resumable SSE, gap-resync watermark, bounded retention, 32-stream cap | Production RBAC, multi-replica stream limits, reconnect/load campaigns and accessibility audit |
| P2 — Approval boundary | Exact-plan digest, identity-bound grant/expiry/revocation, reviewer epoch and atomic consume-on-send | OIDC SPA callback implementation, external IdP integration tests, per-user revocation, independent authority and two-person release policy |
| P3 — Forensic graph | Evidence→hypothesis→plan→effect→outcome lineage, explicitly missing refs | Comprehensive OpenTelemetry span linking and performance checks |
| S1 — Paired replay | Isolated, time-sealed deterministic twin fixtures with an independent postcondition | Actual paired model-vs-runbook controlled trial, statistical inference |
| E1 — Experience capture | Consent-scoped capsules with expiry scrubbing, outcome-reversal quarantine and dependent-lesson revocation | Cross-store deletion and approved classification/retention controls |
| E2 — Outcome adjudication | Separate simulator witness, append-only outcomes and disputes | Independent real-world recovery oracle and delayed outcome reconciliation |
| E3 — Failure learning | Fact-backed cases, superseded-outcome rejection, corrected taxonomy, candidate→rollout invalidation | Independent reviewers, causal attribution beyond typed simulator facts |
| E4 — Validated memory | Typed lesson drafts, source/consent checks, validation/revoke | Release gate for activation, measured retrieval effect and revocation propagation |
| E5 — Evaluation | Frozen paired smoke manifest, integrity checks, negative controls, bounded costs and conservative uncertainty interval | Independent hidden-label store, measured recovery-cost outcomes, powered intervals and separate evaluator authority |
| E6 — Policy rollout | Content-digested drafts, admission-time known-good pinning, cohort preview and disabled rollout | **Live rollout disabled** pending real E5 PASS, signed bundle and release authorization |
| E7 — Strategy routing | Pinned tenant-scoped RUNBOOK/HANDOFF choice, deterministic cost cap | Evaluated contextual policies, measured route accuracy, memory/agent variants |
| E8 — Adapter conformance | Eight disposable simulator checks, code-digest pinning and expiry gate | External adapter conformance/fault-injection/retention campaign |

## Current demonstrated checks

GitHub Actions CI defines four jobs: `test` (SQLite unit/API checks), `frontend` (React/TypeScript build), `temporal-smoke` (native local Temporal replay/signal smoke), and `postgres-temporal-e2e` (native PostgreSQL migrations, disposable conformance, end-to-end simulator incident/recovery, migration downgrade). The build is Docker-free.

What CI **does not** prove: no full production incident recovery, no real external adapter, no load/HA numbers, no trusted production user authentication, no empirical risk calibration and no promoted AI policy. Never report a smoke PASS as deployment approval.

## Conditions to revisit before production

Implement real tenant identity, independently scoped operator/reviewer/release roles, complete audit/secret controls, adapter conformance for each external target, full B1 failure/replay testing, realistic workload budgets, independent held-out E5 measurements, and a signed human-controlled E6 rollout. Live infrastructure effects and learned-policy activation must remain disabled until those safeguards are validated.

## Explicit release blockers

- **Identity and governance:** Production token verification and tenant/role checks have code and mock-key tests, but need real IdP configuration and independent penetration review. Reviewer-policy epochs remain coarse simulator invalidation; least-privilege service identities and independent signing/release roles remain missing.
- **Real-world capability:** No real target adapter is enabled. The simulator is the only effect destination; third-party idempotency/fencing, oracle isolation, audit retention and recovery under crashes are unproven.
- **Measured intelligence:** The optional AI advisory is a constrained, off-by-default read-only prototype. E5 uses public deterministic fixtures, not blinded independent recovery trials; confidence calibration and regression evidence are missing.
- **Operations:** Native PostgreSQL/Temporal CI passes basic migrations and simulator end-to-end flows but does not prove high availability, restore under corruption, multi-host load, penetration resistance, accessibility or disaster recovery.
- **Policy promotion:** E6 active exposure remains deliberately disabled. No smoke PASS, reviewer credential, or successful unit test is a sufficient production release authorization.

The completion definition is **measured acceptance of every relevant test in the PDF**, not the presence of a similarly named file or endpoint.
