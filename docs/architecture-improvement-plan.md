# Incremental architecture improvement plan

This source-level review uses the TITAN-20 feature matrix and existing module boundaries. It is a roadmap, not a claim that all code has been independently audited or production-qualified. Preserve the modular monolith, native PostgreSQL and Temporal, React/TypeScript, existing API contracts and deny-by-default release controls. No Docker or external effect adapters.

| Feature | Proposed next architecture improvement | Regression evidence required |
|---|---|---|
| B1 Durable workflow | Separate orchestration from durable intent/projection contracts; share persisted lifecycle classification | Lost ACK, replay and crash windows |
| B2 Reservations | Keep lease/fence validation in one transactional reservation boundary | Expiry, stale fence and concurrent ownership |
| B3 Effect gateway | Separate receipt-state classification from authorization and simulator transport | UNKNOWN, duplicate send, postcondition and revoked permits |
| B4 Budgets | Pass admission settings explicitly; share classifications with queue health and monitoring | Tenant/global caps, rollback, overload, queue age |
| A1 Investigation | Keep deterministic investigation separate from optional advisory | Model unavailable, bounded calls, deterministic fallback |
| A2 Tool policy | Centralize typed probe contracts and authorization checks at the gateway | Tenant mismatch, replay and budget denial |
| A3 Provenance | Share canonical evidence serialization and verification where semantics match | Tampering, source freshness and tenant isolation |
| A4 Abstention | Separate typed decision evidence from calibrated risk claims | Missing/stale evidence, unsafe proposal denial |
| P1 Workbench | Extract typed HTTP client and focused view/state modules from main.tsx | Snapshot/SSE resync, authorization, keyboard behavior |
| P2 Approvals | Separate API routing from approval domain service; retain atomic grant consumption | Self-approval, expiry, revocation and replay |
| P3 Forensics | Separate lineage response assembly from bounded query and telemetry contracts | Pagination, redaction and cross-tenant denial |
| S1 Paired replay | Keep fixture, execution and score contracts independent | Reproducibility and negative controls |
| E1 Experience | Keep consent/source reads, projection and quarantine in explicit boundaries | Revocation, restricted role and duplicate projection |
| E2 Outcomes | Separate witness input validation from append-only adjudication | Dispute/revision ordering and unavailable witness |
| E3 Failure learning | Separate taxonomy validation from candidate workflow and invalidation | Stale revision and invalidation propagation |
| E4 Memory | Separate lesson eligibility from storage and retrieval | Expiry, consent reversal and target mismatch |
| E5 Evaluation | Share strict bounded input/measurement helpers only where semantics match | Invalid trials, negative controls and signature failures |
| E6 Policy rollout | Keep offline release evidence separate from shadow registry and closed live gate | Invalid signature, rollback, no live promotion |
| E7 Routing | Separate deterministic strategy selection from durable assignment | Cost cap, tenant pin and unsupported strategies |
| E8 Conformance | Separate simulator test harness from offline evidence verification | Fault-result consistency and evidence integrity |

## Execution order

1. Shared lifecycle/configuration boundaries, then focused API domain-router extraction with route and auth contract tests.
2. Frontend typed-client and feature-state extraction without changing interaction contracts.
3. Domain-specific refactors in small commits; do not introduce abstractions for unrelated semantics or change Temporal workflow history without replay checks.
4. Recheck CI after each coherent change. All G1–G6 remain blocked until independently witnessed acceptance evidence exists.

Implemented first slices: shared durable-state classifications used by admission, queue health, age monitoring and recovery audit; explicit request-scoped admission configuration passed from the API into capacity and rate-limit checks, eliminating the benchmark's process-global monkeypatch. This does not alter state transitions, approval, release or dispatch behavior.
