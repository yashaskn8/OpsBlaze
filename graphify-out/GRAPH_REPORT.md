# Graph Report - OpsBlaze  (2026-11-08)

## Corpus Check
- 173 files · ~210,269 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 8 file(s) not represented in the graph (top: (none) 2, .css 2, .example 1)

## Summary
- 2385 nodes · 8439 edges · 103 communities (61 shown, 42 thin omitted)
- Extraction: 82% EXTRACTED · 18% INFERRED · 0% AMBIGUOUS · INFERRED: 1554 edges (avg confidence: 0.91)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- server.cjs
- package.json
- test_b1_api_idempotency.py
- pytest
- models.py
- receipts.py
- uuid
- test_b3_effect_state_machine.py
- test_b1_state_machine.py
- fencing.py
- OutboxDispatcher
- test_e3_failure_taxonomy.py
- test_4_and_5_failure_before_and_during_commit_rolls_back_atomically
- IncidentStatus
- OutboxRepository
- ExperienceRepository
- test_b1_security.py
- incidents.py
- stage3_perf_stress.py
- OutcomeService
- SecretRedactor
- test_stage4_temporal_persistence_recovery.py
- compilerOptions
- gateway.py
- DurableBudgetManager
- env.py
- .admit_incident
- IncidentWorkflow
- outcome.py
- compilerOptions
- stage5_baseline_comparison.py
- conftest.py
- stage4_postgres_restore_drill.py
- test_p1_workbench.py
- IncidentRepository
- stage7.ts
- Incident
- WorkflowIncidentInput
- test_stage4_postgres_migration_and_restore.py
- helper.js
- s1_replay.py
- sqlalchemy
- stop-server.sh
- .oxlintrc.json
- improvements.py
- test_stage7_complete_e1_e2_lifecycle_and_view_integration
- start-server.sh
- quality-gate.mjs
- conformance.py
- tsconfig.json
- find-polluter.sh
- opsblaze
- api/trace.py
- recovery.py
- App.tsx
- stage6.ts
- main.py
- lessons.py
- emit_event
- WorkbenchView.tsx
- react
- trace/service.py
- scripts
- test_e4_lesson_registry.py
- metrics.py
- LessonCitation
- workbench/service.py
- IncidentCreateRequest
- DomainError
- TargetComponent
- stage8.ts
- EffectReceipt
- test_stage8_e3_e4_lifecycle.py
- typing
- canonical_request_hash

## God Nodes (most connected - your core abstractions)
1. `IncidentRepository` - 155 edges
2. `IncidentStatus` - 85 edges
3. `DisposableTargetSimulator` - 78 edges
4. `emit_event()` - 67 edges
5. `Incident` - 62 edges
6. `TargetAdapterProtocol` - 47 edges
7. `IncidentWorkflow` - 46 edges
8. `ResourceReservationManager` - 46 edges
9. `ApprovalService` - 45 edges
10. `B1Activities` - 44 edges

## Surprising Connections (you probably didn't know these)
- `get_session_factory()` --indirect_call--> `session_factory()`  [INFERRED]
  backend/api/improvements.py → tests/conftest.py
- `get_session_factory()` --indirect_call--> `session_factory()`  [INFERRED]
  backend/api/lessons.py → tests/conftest.py
- `test_adversarial_synthetic_incident_exclusion()` --uses--> `FailureClassificationStatus`  [INFERRED]
  tests/chaos/test_stage8_omega_adversarial.py → backend/domain/failure.py
- `test_adversarial_forbidden_proposal_diff_tampering()` --uses--> `DivergentStepCategory`  [INFERRED]
  tests/chaos/test_stage8_omega_adversarial.py → backend/domain/failure.py
- `test_stage8_complete_e3_e4_p3_lifecycle()` --uses--> `DivergentStepCategory`  [INFERRED]
  tests/integration/test_stage8_e3_e4_lifecycle.py → backend/domain/failure.py

## Import Cycles
- None detected.

## Communities (103 total, 42 thin omitted)

### Community 0 - "server.cjs"
Cohesion: 0.05
Nodes (57): bootstrapPage(), brandMarkup(), broadcast(), browserLauncherForPlatform(), chmodOwnerOnly(), clients, companionUrl(), computeAcceptKey() (+49 more)

### Community 1 - "package.json"
Cohesion: 0.13
Nodes (19): devDependencies, oxlint@^1.90.0, @types/node@^24.19.2, @types/react@^19.3.0, @types/react-dom@^19.3.0, typescript@~7.0.2, vite@^8.3.6, @vitejs/plugin-react@^6.1.2 (+11 more)

### Community 2 - "test_b1_api_idempotency.py"
Cohesion: 0.25
Nodes (10): _count_incidents_and_outbox(), test_10_duplicate_requests_following_api_process_restart(), test_11_api_cancellation_stale_expected_version_and_concurrent_race(), test_1_same_key_same_payload_sequentially(), test_2_same_key_same_payload_concurrently_barrier(), test_3_same_key_different_payload_sequentially_returns_409(), test_4_same_key_different_payload_concurrently(), test_5_multiple_tenants_using_identical_keys() (+2 more)

### Community 3 - "pytest"
Cohesion: 0.10
Nodes (15): ProposalType, sanitize_lesson_summary_for_llm(), FailureClusterService, LessonRetriever, LessonUsage, OutcomeRevision, _create_test_hierarchy(), test_adversarial_cas_optimistic_locking_races() (+7 more)

### Community 4 - "models.py"
Cohesion: 0.12
Nodes (11): get_settings(), make_engine(), make_session_factory(), _get_process_memory_mb(), _percentile(), run_benchmark(), main(), main() (+3 more)

### Community 5 - "receipts.py"
Cohesion: 0.10
Nodes (14): TargetAdapterProtocol, ActionGateway, compute_effect_idempotency_key(), EffectIdempotencyConflictError, EffectReceiptError, EffectReceiptSnapshot, EffectState, IndependentPostconditionVerifier (+6 more)

### Community 6 - "uuid"
Cohesion: 0.10
Nodes (8): BudgetStatus, AbstentionReason, CalibratedAbstentionGate, GateDecision, test_abstention_gate_fires_on_budget_exhaustion(), test_cold_start_continuation_allows_probing_at_step_one(), test_gate1_allows_proposal_when_calibrated_confidence_met(), test_gate1_rejects_proposal_when_margin_is_too_small()

### Community 7 - "test_b3_effect_state_machine.py"
Cohesion: 0.12
Nodes (9): ConformanceRecordSnapshot, ConformanceStatus, MutationCapabilityGate, EffectReconciler, AdapterConformance, _admit_incident_to_executing(), certified_b3_simulator(), test_b3_false_success_partial_effect_retention_expiry_and_lookup_outage() (+1 more)

### Community 8 - "test_b1_state_machine.py"
Cohesion: 0.12
Nodes (13): IncidentStatus, InvalidStateTransitionError, StaleStateVersionError, validate_client_key(), validate_transition(), test_cancellable_vs_non_cancellable_states(), test_every_illegal_transition_rejected(), test_every_legal_transition_succeeds() (+5 more)

### Community 9 - "fencing.py"
Cohesion: 0.09
Nodes (20): ApprovalRequiredError, InvalidReservationStateTransitionError, MutationPermit, ReservationError, ReservationExpiredError, ReservationState, ResourceReservationManager, StaleFenceError (+12 more)

### Community 10 - "OutboxDispatcher"
Cohesion: 0.14
Nodes (10): make_incident_workflow_id(), ClaimedOutboxItem, OutboxDispatcher, FakeTemporalStarter, _get_outbox_for_incident(), test_crash_windows_1_to_4_before_claim_after_claim_and_during_temporal_start(), test_critical_scenario_crash_windows_5_to_9_lost_ack_and_reclaim_idempotency(), test_stuck_outbox_detection_emits_stuck_event_and_metric() (+2 more)

### Community 11 - "test_e3_failure_taxonomy.py"
Cohesion: 0.10
Nodes (20): BudgetTuningDiff, DivergentStepCategory, FailureCaseNotFoundError, FailureClassificationStatus, FailureClusterStatus, FailureOriginCode, PromptRefinementDiff, ProposalStatus (+12 more)

### Community 12 - "test_4_and_5_failure_before_and_during_commit_rolls_back_atomically"
Cohesion: 0.11
Nodes (9): _count_tenant_state(), test_1_failure_before_incident_insertion_leaves_zero_rows(), test_2_failure_during_incident_insertion_rolls_back(), test_3_failure_during_outbox_insertion_rolls_back_incident(), test_4_and_5_failure_before_and_during_commit_rolls_back_atomically(), wrapped_factory(), test_6_failure_immediately_after_successful_commit_preserves_idempotent_recovery(), test_9_connection_pool_exhaustion_and_recovery() (+1 more)

### Community 13 - "IncidentStatus"
Cohesion: 0.11
Nodes (9): TemporalWorkflowStarter, create_b1_worker(), _find_free_port(), test_temporal_activity_failure_timeout_retry_and_worker_restart(), flaky_investigation(), test_temporal_end_to_end_workflow_progression_cancel_and_replay(), test_temporal_server_restart_with_persisted_db_file(), test_temporal_unavailable_during_dispatch_and_cancellation_race_with_failed_resolution() (+1 more)

### Community 15 - "ExperienceRepository"
Cohesion: 0.08
Nodes (18): export_experiences(), get_authenticated_tenant_id(), get_consent_manager(), get_consent_status(), get_experience_repository(), get_session_factory(), _handle_experience_error(), list_experiences() (+10 more)

### Community 16 - "test_b1_security.py"
Cohesion: 0.29
Nodes (5): test_cross_tenant_read_and_cancel_denied_with_404(), test_forged_tenant_id_or_workflow_id_in_body_rejected_with_422(), test_log_injection_and_secret_redaction_never_expose_env_values(), test_missing_malformed_or_unregistered_tenant_headers_denied(), test_oversized_payload_and_malformed_json_rejected()

### Community 17 - "incidents.py"
Cohesion: 0.19
Nodes (12): _attach_investigation_if_applicable(), cancel_incident(), create_incident(), get_authenticated_tenant_id(), get_idempotency_key(), get_incident(), get_session_factory(), IdempotencyConflictError (+4 more)

### Community 18 - "stage3_perf_stress.py"
Cohesion: 0.15
Nodes (15): FaultRule, ApprovedPlanContext, ResourceAlreadyReservedError, ResourceUncertainEffectPendingError, _percentile(), run_benchmark(), _diff_worker(), _same_worker() (+7 more)

### Community 19 - "OutcomeService"
Cohesion: 0.06
Nodes (15): compute_observation_digest(), ConfidenceBand, InvalidOutcomeTransitionError, ObservationResponse, OperatorFeedbackRequest, OutcomeConflictError, OutcomeError, OutcomeLabel (+7 more)

### Community 21 - "test_stage4_temporal_persistence_recovery.py"
Cohesion: 0.23
Nodes (4): _find_free_port(), test_1_temporal_sqlite_online_backup_and_restart_recovery(), test_2_stage3_approved_execution_across_temporal_server_restart(), _wait_for_status()

### Community 22 - "compilerOptions"
Cohesion: 0.10
Nodes (20): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+12 more)

### Community 23 - "gateway.py"
Cohesion: 0.06
Nodes (27): CapabilityExpiredError, CapabilityGrantSpec, CapabilityRevokedError, MutationToolRejectedError, OutputLimitExceededError, ToolCallRequest, ToolCallResult, ToolDefinition (+19 more)

### Community 24 - "DurableBudgetManager"
Cohesion: 0.10
Nodes (14): DurableBudgetManager, BudgetConcurrencyError, BudgetError, BudgetExhaustedError, BudgetLimits, BudgetReservation, BudgetReservationNotFoundError, BudgetSnapshot (+6 more)

### Community 25 - "env.py"
Cohesion: 0.17
Nodes (6): _get_url_str(), _run(), _run_async_migrations(), run_migrations_offline(), run_migrations_online(), test_stage7_schema_and_historical_data_integrity()

### Community 26 - ".admit_incident"
Cohesion: 0.06
Nodes (38): approve_plan(), create_plan_approval(), get_approval_by_id(), _handle_approval_error(), reject_plan(), revoke_plan_approval(), ApprovalService, TemporalApprovalSignalerProtocol (+30 more)

### Community 27 - "IncidentWorkflow"
Cohesion: 0.06
Nodes (25): ApprovedExecutionSignalInput, Stage3DispatchActivityOutput, Stage3VerifyActivityOutput, StatusProjectionActivityInput, StatusProjectionActivityOutput, ActionSpec, AdaptiveInvestigationActivityInput, AdaptiveInvestigationActivityOutput (+17 more)

### Community 28 - "outcome.py"
Cohesion: 0.13
Nodes (8): get_authenticated_tenant_id(), get_incident_outcome(), get_outcome_service(), get_session_factory(), _handle_outcome_error(), record_observation(), submit_operator_feedback(), OutcomeNotFoundError

### Community 29 - "compilerOptions"
Cohesion: 0.11
Nodes (17): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+9 more)

### Community 30 - "stage5_baseline_comparison.py"
Cohesion: 0.06
Nodes (22): HypothesisItem, InvestigationLoopState, InvestigationModelResponse, InvestigationResult, ModelAdapterProtocol, ProbeSelection, ProposedPlan, DeterministicFakeModelAdapter (+14 more)

### Community 31 - "conftest.py"
Cohesion: 0.12
Nodes (16): Settings, _worker(), _worker_recovery(), query_worker(), revoker(), api_client(), db_engine(), isolated_tenant() (+8 more)

### Community 32 - "stage4_postgres_restore_drill.py"
Cohesion: 0.17
Nodes (10): _create_isolated_db(), _drop_isolated_db(), inspect_database_fingerprint(), _norm_pg_expr(), _make_async_engine(), _pg_env(), run_drill(), _upgrade_isolated_db_to_head() (+2 more)

### Community 33 - "test_p1_workbench.py"
Cohesion: 0.35
Nodes (5): ToolCatalog, test_p1_workbench_abstention_detection(), test_p1_workbench_missing_incident_returns_404(), test_p1_workbench_snapshot_empty_and_populated(), test_p1_workbench_sse_stream_events_and_heartbeats()

### Community 34 - "IncidentRepository"
Cohesion: 0.19
Nodes (20): ObservationCreateRequest, OutcomeService, IncidentRepository, test_adversarial_cas_concurrent_dispute_resolution(), test_adversarial_consent_race_concurrent_reads(), test_adversarial_cross_tenant_isolation_gate(), test_adversarial_delayed_relapse_injection(), test_adversarial_model_self_grading_evasion_blocked() (+12 more)

### Community 35 - "stage7.ts"
Cohesion: 0.14
Nodes (13): ConfidenceBand, ConsentStatusResponse, ConsentUpdateRequest, ExperienceCapsuleResponse, ExperienceClassification, ExperienceListResponse, ExperienceState, ObservationResponse (+5 more)

### Community 37 - "Incident"
Cohesion: 0.08
Nodes (15): ContextManifest, EvidenceClassification, EvidenceRecord, EvidenceValidity, ContextManifestBuilder, EvidenceLedgerService, Incident, test_adversarial_cross_tenant_evidence_tampering_is_rejected() (+7 more)

### Community 38 - "WorkflowIncidentInput"
Cohesion: 0.14
Nodes (8): InvestigationObservation, InvestigationResult, WorkflowIncidentInput, FakeInvestigator, WorkflowStarterProtocol, WorkflowStateSnapshot, test_fake_investigator_cannot_mutate_or_accept_command_injection(), test_fake_investigator_determinism_and_read_only_contract()

### Community 40 - "helper.js"
Cohesion: 0.42
Nodes (7): connect(), nextReconnectDelay(), reloadAfterRecovery(), sessionKey(), setStatus(), showTombstone(), websocketUrl()

### Community 41 - "s1_replay.py"
Cohesion: 0.07
Nodes (19): HiddenLabel, IndependentOracle, DiagnosticFixture, EvaluationReport, generate_standard_fixture_suite(), StrategyExecutor, TimeSealedDecisionReplayHarness, TrialResult (+11 more)

### Community 42 - "sqlalchemy"
Cohesion: 0.18
Nodes (10): upgrade(), downgrade(), upgrade(), upgrade(), downgrade(), upgrade(), downgrade(), upgrade() (+2 more)

### Community 43 - "stop-server.sh"
Cohesion: 0.52
Nodes (6): command_has_server_id(), command_line_for_pid(), is_brainstorm_server(), mark_stopped(), read_expected_server_id(), stop-server.sh script

### Community 44 - ".oxlintrc.json"
Cohesion: 0.33
Nodes (5): plugins, rules, react/only-export-components, react/rules-of-hooks, $schema

### Community 45 - "improvements.py"
Cohesion: 0.08
Nodes (22): create_improvement_draft(), DraftProposalRequest, FailureCaseResponse, FailureClusterResponse, get_authenticated_tenant_id(), get_failure_cluster(), get_session_factory(), ImprovementProposalResponse (+14 more)

### Community 47 - "test_stage7_complete_e1_e2_lifecycle_and_view_integration"
Cohesion: 0.09
Nodes (11): ConsentStatusResponse, ConsentManager, ExperienceProjector, ConsentEpoch, test_stage7_complete_e1_e2_lifecycle_and_view_integration(), test_e1_api_routes(), test_e1_consent_lifecycle_and_epoch_advance(), test_e1_experience_projection_and_idempotency() (+3 more)

### Community 51 - "conformance.py"
Cohesion: 0.07
Nodes (29): AdapterConformanceError, AdapterDigestMismatchError, AdapterEnablementExpiredError, AdapterNotConformantError, AdapterQuarantinedError, ConformanceReport, DefectiveStaleFenceSimulatorAdapter, DefectiveVersionCasSimulatorAdapter (+21 more)

### Community 68 - "recovery.py"
Cohesion: 0.08
Nodes (20): ConformanceRunner, DegradedRecoveryGate, Base, EffectReceipt, Outbox, ResourceEpoch, ResourceReservation, Tenant (+12 more)

### Community 69 - "App.tsx"
Cohesion: 0.20
Nodes (12): App(), CANCELLABLE_STATES, generateIdempotencyKey(), getInitialIncidentId(), getInitialTenantId(), IncidentResponse, IncidentStatus, InvestigationObservation (+4 more)

### Community 70 - "stage6.ts"
Cohesion: 0.16
Nodes (12): Props, TraceExplorerView(), AbstentionDetail, ApprovalState, BudgetHeadroom, EvidenceItem, HypothesisItem, InvestigationStepItem (+4 more)

### Community 71 - "main.py"
Cohesion: 0.24
Nodes (4): create_app(), readyz(), _ensure_app_engine(), lifespan()

### Community 72 - "lessons.py"
Cohesion: 0.15
Nodes (12): draft_lesson(), DraftLessonRequest, get_authenticated_tenant_id(), get_session_factory(), list_lessons(), OperationalLessonResponse, revoke_lesson(), RevokeLessonRequest (+4 more)

### Community 73 - "emit_event"
Cohesion: 0.07
Nodes (15): validate_reviewer_role(), CapsuleNotFoundError, CapsuleRedactionError, ExperienceAuditAction, ExperienceCapsuleResponse, ExperienceClassification, ExperienceState, ExperienceRedactor (+7 more)

### Community 74 - "WorkbenchView.tsx"
Cohesion: 0.32
Nodes (6): ApprovalCockpitView(), Props, Props, WorkbenchView(), ApprovalResponse, WorkbenchSnapshotResponse

### Community 75 - "react"
Cohesion: 0.33
Nodes (5): dependencies, react@^19.3.1, react-dom@^19.3.1, react, react-dom

### Community 76 - "trace/service.py"
Cohesion: 0.20
Nodes (8): TraceEdge, TraceGraphResponse, TraceNode, TraceNodeKind, TraceRelation, FailureCluster, _detect_cycles(), TraceService

### Community 77 - "scripts"
Cohesion: 0.40
Nodes (5): scripts, build, dev, lint, preview

### Community 79 - "test_e4_lesson_registry.py"
Cohesion: 0.09
Nodes (17): LessonConfidenceBand, LessonConflictError, LessonLifecycle, LessonNotFoundError, LessonType, LessonValidationError, LessonRegistry, OperationalLesson (+9 more)

### Community 80 - "metrics.py"
Cohesion: 0.12
Nodes (4): correlation_and_size_guard(), validation_exception_handler(), MetricsRegistry, redact_value()

### Community 83 - "workbench/service.py"
Cohesion: 0.09
Nodes (11): get_workbench_snapshot(), stream_incident_events(), AbstentionDetail, BudgetHeadroom, EvidenceItem, HypothesisItem, InvestigationStepItem, ToolCallItem (+3 more)

### Community 89 - "TargetComponent"
Cohesion: 0.14
Nodes (10): ProposalValidator, TargetComponent, test_proposal_validator_allows_benign_english_verbs(), test_proposal_validator_rejects_extra_fields_in_diff(), test_proposal_validator_rejects_forbidden_component(), test_proposal_validator_rejects_python_code_smuggling(), test_proposal_validator_rejects_sql_smuggling(), test_proposal_validator_scans_variable_deltas_for_smuggling() (+2 more)

### Community 90 - "stage8.ts"
Cohesion: 0.14
Nodes (13): DivergentStepCategory, FailureCaseResponse, FailureClassificationStatus, FailureClusterResponse, FailureClusterStatus, ImprovementProposalResponse, LessonCitation, LessonLifecycle (+5 more)

### Community 91 - "EffectReceipt"
Cohesion: 0.30
Nodes (6): ApprovalRequest, EvidenceLedger, test_p3_trace_graph_cross_incident_receipt_isolation_and_digest_matching(), test_p3_trace_graph_dag_projection_and_acyclicity(), test_p3_trace_graph_missing_incident_returns_404(), test_p3_trace_graph_missing_link_annotations()

### Community 93 - "typing"
Cohesion: 0.22
Nodes (3): test_historical_data_preservation_opsblaze(), test_historical_data_preservation_opsblaze_test(), test_stage8_tables_and_columns_exist()

## Knowledge Gaps
- **2 isolated node(s):** `RFC-6455`, `opsblaze`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 961 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **42 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `IncidentRepository` connect `IncidentRepository` to `test_b1_api_idempotency.py`, `pytest`, `models.py`, `receipts.py`, `test_b3_effect_state_machine.py`, `test_b1_state_machine.py`, `fencing.py`, `OutboxDispatcher`, `test_e3_failure_taxonomy.py`, `test_4_and_5_failure_before_and_during_commit_rolls_back_atomically`, `IncidentStatus`, `OutboxRepository`, `incidents.py`, `stage3_perf_stress.py`, `OutcomeService`, `test_stage4_temporal_persistence_recovery.py`, `gateway.py`, `.admit_incident`, `IncidentWorkflow`, `conftest.py`, `test_p1_workbench.py`, `Incident`, `test_stage7_complete_e1_e2_lifecycle_and_view_integration`, `recovery.py`, `main.py`, `emit_event`, `trace/service.py`, `test_e4_lesson_registry.py`, `workbench/service.py`, `EffectReceipt`, `test_stage8_e3_e4_lifecycle.py`, `.__init__`?**
  _High betweenness centrality (0.109) - this node is a cross-community bridge._
- **Are the 22 inferred relationships involving `IncidentRepository` (e.g. with `cancel_incident()` and `create_incident()`) actually correct?**
  _`IncidentRepository` has 22 INFERRED edges - model-reasoned connections that need verification._
- **What connects `RFC-6455`, `opsblaze` to the rest of the system?**
  _2 weakly-connected nodes found - possible missing edges._
- **Should `server.cjs` be split into smaller, more focused modules?**
  _Cohesion score 0.05034965034965035 - nodes in this community are weakly interconnected._
- **Why does `Incident` connect `Incident` to `test_b1_api_idempotency.py`, `models.py`, `test_b1_state_machine.py`, `fencing.py`, `OutboxDispatcher`, `test_e3_failure_taxonomy.py`, `test_4_and_5_failure_before_and_during_commit_rolls_back_atomically`, `OutboxRepository`, `stage3_perf_stress.py`, `OutcomeService`, `gateway.py`, `DurableBudgetManager`, `.admit_incident`, `stage5_baseline_comparison.py`, `conftest.py`, `IncidentRepository`, `test_stage7_complete_e1_e2_lifecycle_and_view_integration`, `recovery.py`, `emit_event`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Are the 47 inferred relationships involving `IncidentStatus` (e.g. with `_attach_investigation_if_applicable()` and `cancel_incident()`) actually correct?**
  _`IncidentStatus` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Should `package.json` be split into smaller, more focused modules?**
  _Cohesion score 0.13333333333333333 - nodes in this community are weakly interconnected._