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

API router extraction: readiness, tenant queue snapshots and projection dead-letter review/redrive HTTP endpoints now live in `backend/routers/operations.py`. Paths, methods, dependencies and status codes are pinned by a registration regression test; underlying service behavior and privileges are unchanged.

Frontend extraction: typed read-only operations requests and their response types now live in `frontend/src/api.ts`. Queue/dead-letter state, stale-response fencing and all mutation permission checks remain in the workbench. Node contract tests stub fetch; CI runs them before the existing strict TypeScript/Vite build. TypeScript types describe the existing wire format and are not a runtime schema validator.

Workbench request ownership (P1–P3). A disposable browser fixture reproduced a prior incident remaining visible after changing the tenant during a pending refresh. The workbench now clears identity-scoped data before paint and cancels requests when their credentials or incident selection are superseded. Manual snapshots and the event stream share the same incident version check. A keyed remount of the entire workbench would reset the credential inputs and their keyboard focus. Explicit request ownership retains the existing form and preserves server-side authorization and mutation semantics. Playwright verified that the prior incident and recent list cleared, the delayed refresh could not restore them, and focus stayed on the tenant field. A second browser check confirmed that changing the selected incident within one tenant preserves the newer selection after the older refresh returns. Spoken accessibility acceptance remains deferred.

Database readiness deadline (B4). A stalled probe previously outlived the synthetic caller's one-second deadline. The readiness route now gives its database checks three seconds, reuses the existing 503 response on failure, and retains its rollback before probing Temporal. A global driver timeout would also affect business transactions, and a caller-only timeout would leave readiness query ownership outside the route. A unit test verifies cancellation and recovery, and the native fixture exercises a stalled PostgreSQL statement and reuse of a one-connection pool. The three-second budget applies to the database phase and excludes request teardown and the separate Temporal probe.

Ten warmed calls per revision exercised the same synthetic stalled probe. The starting revision reached the caller's 400 ms deadline in every run (median 412.5 ms, range 408.9–414.3 ms). With a 150 ms test budget, every repaired call returned HTTP 503 (median 158.1 ms, range 155.5–159.5 ms). This measures failure handling under an injected stall. The runtime database-phase budget is three seconds.

The starting checkpoint `ec109be` passed [all ten Linux CI jobs](https://github.com/yashaskn8/OpsBlaze/actions/runs/45818261615). Local checks for this increment passed six Python import contracts, the frontend dependency rules, and both isolated forbidden-import controls. The Windows-compatible suite passed 765 backend tests with 78.75% branch-aware coverage. Native checks passed 29 PostgreSQL and 11 separate Temporal integration tests. The frontend passed 17 tests and the production build. Browser checks included keyboard approval, rejection, expiry, a competing decision returning HTTP 409, and recovery after a stream disconnect. These results extend the existing functional checkpoint. Production evidence and E4 effectiveness retain their existing acceptance criteria.
