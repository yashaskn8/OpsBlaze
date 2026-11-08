# Graph Report - OpsBlaze  (2026-11-08)

## Corpus Check
- 173 files · ~207,754 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 8 file(s) not represented in the graph (top: (none) 2, .css 2, .example 1)

## Summary
- 2367 nodes · 8587 edges · 99 communities (64 shown, 35 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 1744 edges (avg confidence: 0.92)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- server.cjs
- package.json
- test_b1_api_idempotency.py
- test_stage8_omega_adversarial.py
- models.py
- receipts.py
- GateDecision
- test_b3_effect_state_machine.py
- test_b1_state_machine.py
- fencing.py
- OutboxDispatcher
- test_e3_failure_taxonomy.py
- test_4_and_5_failure_before_and_during_commit_rolls_back_atomically
- IncidentStatus
- DisposableTargetSimulator
- ExperienceRepository
- test_stage5_investigation_workflow.py
- incidents.py
- stage3_perf_stress.py
- OutcomeService
- activities.py
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
- models.py
- stage4_postgres_restore_drill.py
- InvestigationStep
- .admit_incident
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
- test_stage7_e1_e2_lifecycle.py
- start-server.sh
- quality-gate.mjs
- conformance.py
- tsconfig.json
- find-polluter.sh
- opsblaze
- stream_incident_events
- recovery.py
- App.tsx
- stage6.ts
- main.py
- lessons.py
- test_e1_experience_capture.py
- WorkbenchView.tsx
- react
- trace/service.py
- scripts
- test_e4_lesson_registry.py
- sqlalchemy_ext_asyncio
- LessonRetriever
- workbench/service.py
- lesson_registry.py
- failure.py
- ProposalType
- stage8.ts
- EffectReceipt
- test_stage8_e3_e4_lifecycle.py
- test_stage8_tables_and_columns_exist

## God Nodes (most connected - your core abstractions)
1. `IncidentRepository` - 156 edges
2. `IncidentStatus` - 85 edges
3. `DisposableTargetSimulator` - 78 edges
4. `Incident` - 69 edges
5. `emit_event()` - 67 edges
6. `TargetAdapterProtocol` - 47 edges
7. `ResourceReservationManager` - 46 edges
8. `IncidentWorkflow` - 46 edges
9. `OutcomeService` - 45 edges
10. `ApprovalService` - 45 edges

## Surprising Connections (you probably didn't know these)
- `_worker()` --calls--> `session_factory()`  [INFERRED]
  scripts/investigate_receipt_latency.py → tests/conftest.py
- `get_session_factory()` --indirect_call--> `session_factory()`  [INFERRED]
  backend/api/improvements.py → tests/conftest.py
- `get_session_factory()` --indirect_call--> `session_factory()`  [INFERRED]
  backend/api/lessons.py → tests/conftest.py
- `test_adversarial_synthetic_incident_exclusion()` --uses--> `FailureClassificationStatus`  [INFERRED]
  tests/chaos/test_stage8_omega_adversarial.py → backend/domain/failure.py
- `test_adversarial_forbidden_proposal_diff_tampering()` --uses--> `DivergentStepCategory`  [INFERRED]
  tests/chaos/test_stage8_omega_adversarial.py → backend/domain/failure.py

## Import Cycles
- None detected.

## Communities (99 total, 35 thin omitted)

### Community 0 - "server.cjs"
Cohesion: 0.05
Nodes (57): bootstrapPage(), brandMarkup(), broadcast(), browserLauncherForPlatform(), chmodOwnerOnly(), clients, companionUrl(), computeAcceptKey() (+49 more)

### Community 1 - "package.json"
Cohesion: 0.13
Nodes (19): devDependencies, oxlint@^1.90.0, @types/node@^24.19.2, @types/react@^19.3.0, @types/react-dom@^19.3.0, typescript@~7.0.2, vite@^8.3.6, @vitejs/plugin-react@^6.1.2 (+11 more)

### Community 2 - "test_b1_api_idempotency.py"
Cohesion: 0.21
Nodes (12): _count_incidents_and_outbox(), test_10_duplicate_requests_following_api_process_restart(), test_11_api_cancellation_stale_expected_version_and_concurrent_race(), cancel_worker(), transition_worker(), test_1_same_key_same_payload_sequentially(), test_2_same_key_same_payload_concurrently_barrier(), test_3_same_key_different_payload_sequentially_returns_409() (+4 more)

### Community 3 - "test_stage8_omega_adversarial.py"
Cohesion: 0.12
Nodes (12): FailureClusterNotFoundError, ProposalValidationError, FailureClusterService, ProposalService, FailureCluster, _create_test_hierarchy(), test_adversarial_cas_optimistic_locking_races(), test_adversarial_forbidden_proposal_diff_tampering() (+4 more)

### Community 4 - "models.py"
Cohesion: 0.10
Nodes (14): get_settings(), make_engine(), make_session_factory(), _get_process_memory_mb(), _percentile(), run_benchmark(), main(), _worker() (+6 more)

### Community 5 - "receipts.py"
Cohesion: 0.12
Nodes (14): MutationPermit, ActionGateway, compute_effect_idempotency_key(), EffectIdempotencyConflictError, EffectReceiptError, EffectReceiptSnapshot, EffectState, IndependentPostconditionVerifier (+6 more)

### Community 7 - "test_b3_effect_state_machine.py"
Cohesion: 0.16
Nodes (5): EffectReconciler, _admit_incident_to_executing(), certified_b3_simulator(), test_b3_false_success_partial_effect_retention_expiry_and_lookup_outage(), test_b3_happy_path_duplicate_dispatch_and_lost_response_reconciliation()

### Community 8 - "test_b1_state_machine.py"
Cohesion: 0.08
Nodes (18): canonical_request_hash(), DomainError, IncidentCreateRequest, IncidentStatus, InvalidStateTransitionError, _normalize_canonical_value(), StaleStateVersionError, validate_client_key() (+10 more)

### Community 9 - "fencing.py"
Cohesion: 0.09
Nodes (19): ApprovalRequiredError, InvalidReservationStateTransitionError, ReservationError, ReservationExpiredError, ResourceAlreadyReservedError, ResourceReservationManager, StaleFenceError, TargetVersionConflictError (+11 more)

### Community 10 - "OutboxDispatcher"
Cohesion: 0.09
Nodes (13): make_incident_workflow_id(), ClaimedOutboxItem, OutboxRepository, OutboxDispatcher, WorkflowStarterProtocol, FakeTemporalStarter, _get_outbox_for_incident(), test_crash_windows_1_to_4_before_claim_after_claim_and_during_temporal_start() (+5 more)

### Community 11 - "test_e3_failure_taxonomy.py"
Cohesion: 0.15
Nodes (12): DivergentStepCategory, FailureClassificationStatus, FailureOriginCode, ProposalStatus, FailureClassifier, _create_test_hierarchy(), test_failure_classifier_disputed_outcome(), test_failure_classifier_ignores_evaluation_incidents() (+4 more)

### Community 12 - "test_4_and_5_failure_before_and_during_commit_rolls_back_atomically"
Cohesion: 0.11
Nodes (9): _count_tenant_state(), test_1_failure_before_incident_insertion_leaves_zero_rows(), test_2_failure_during_incident_insertion_rolls_back(), test_3_failure_during_outbox_insertion_rolls_back_incident(), test_4_and_5_failure_before_and_during_commit_rolls_back_atomically(), wrapped_factory(), test_6_failure_immediately_after_successful_commit_preserves_idempotent_recovery(), test_9_connection_pool_exhaustion_and_recovery() (+1 more)

### Community 13 - "IncidentStatus"
Cohesion: 0.13
Nodes (9): TemporalWorkflowStarter, create_b1_worker(), _find_free_port(), test_temporal_activity_failure_timeout_retry_and_worker_restart(), flaky_investigation(), test_temporal_end_to_end_workflow_progression_cancel_and_replay(), test_temporal_server_restart_with_persisted_db_file(), test_temporal_unavailable_during_dispatch_and_cancellation_race_with_failed_resolution() (+1 more)

### Community 14 - "DisposableTargetSimulator"
Cohesion: 0.16
Nodes (8): DefectiveStaleFenceSimulatorAdapter, DefectiveVersionCasSimulatorAdapter, AdapterApplyRequest, AdapterApplyResponse, DisposableTargetSimulator, RecordedSimulatorEffect, SimulatedTargetResource, test_e8_simulator_protocol_idempotency_fencing_and_version_cas()

### Community 15 - "ExperienceRepository"
Cohesion: 0.07
Nodes (20): export_experiences(), get_authenticated_tenant_id(), get_consent_manager(), get_consent_status(), get_experience_repository(), get_session_factory(), _handle_experience_error(), list_experiences() (+12 more)

### Community 16 - "test_stage5_investigation_workflow.py"
Cohesion: 0.30
Nodes (3): test_stage5_temporal_adaptive_investigation_abstains_to_needs_review(), test_stage5_temporal_adaptive_investigation_proposes_plan(), _wait_for_incident_status()

### Community 17 - "incidents.py"
Cohesion: 0.12
Nodes (12): _attach_investigation_if_applicable(), cancel_incident(), create_incident(), get_authenticated_tenant_id(), get_idempotency_key(), get_incident(), get_session_factory(), IdempotencyConflictError (+4 more)

### Community 18 - "stage3_perf_stress.py"
Cohesion: 0.12
Nodes (16): ApprovedPlanContext, ResourceUncertainEffectPendingError, ActionSpec, generate_all(), _percentile(), run_benchmark(), _diff_worker(), _same_worker() (+8 more)

### Community 19 - "OutcomeService"
Cohesion: 0.10
Nodes (21): compute_observation_digest(), ConfidenceBand, ObservationCreateRequest, ObservationResponse, OutcomeConflictError, OutcomeLabel, OutcomeRevisionResponse, OutcomeSummaryResponse (+13 more)

### Community 20 - "activities.py"
Cohesion: 0.11
Nodes (6): AdaptiveProbePlanner, AdaptiveInvestigationActivityInput, AdaptiveInvestigationActivityOutput, B1Activities, run_benchmark(), StrategyMetrics

### Community 21 - "test_stage4_temporal_persistence_recovery.py"
Cohesion: 0.24
Nodes (4): _find_free_port(), test_1_temporal_sqlite_online_backup_and_restart_recovery(), test_2_stage3_approved_execution_across_temporal_server_restart(), _wait_for_status()

### Community 22 - "compilerOptions"
Cohesion: 0.10
Nodes (20): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+12 more)

### Community 23 - "gateway.py"
Cohesion: 0.05
Nodes (25): CapabilityExpiredError, CapabilityGrantSpec, CapabilityRevokedError, MutationToolRejectedError, OutputLimitExceededError, ToolCallRequest, ToolCallResult, ToolDefinition (+17 more)

### Community 24 - "DurableBudgetManager"
Cohesion: 0.08
Nodes (18): DurableBudgetManager, BudgetConcurrencyError, BudgetError, BudgetExhaustedError, BudgetLimits, BudgetReservation, BudgetReservationNotFoundError, BudgetSnapshot (+10 more)

### Community 25 - "env.py"
Cohesion: 0.36
Nodes (5): _get_url_str(), _run(), _run_async_migrations(), run_migrations_offline(), run_migrations_online()

### Community 26 - ".admit_incident"
Cohesion: 0.05
Nodes (40): approve_plan(), create_plan_approval(), get_approval_by_id(), _handle_approval_error(), reject_plan(), revoke_plan_approval(), ApprovalService, TemporalApprovalSignalerProtocol (+32 more)

### Community 27 - "IncidentWorkflow"
Cohesion: 0.06
Nodes (17): ApprovedExecutionSignalInput, Stage3DispatchActivityOutput, Stage3VerifyActivityOutput, StatusProjectionActivityInput, StatusProjectionActivityOutput, FakeInvestigationActivityOutput, IncidentWorkflow, WorkflowStateSnapshot (+9 more)

### Community 28 - "outcome.py"
Cohesion: 0.10
Nodes (11): get_authenticated_tenant_id(), get_incident_outcome(), get_outcome_service(), get_session_factory(), _handle_outcome_error(), record_observation(), submit_operator_feedback(), InvalidOutcomeTransitionError (+3 more)

### Community 29 - "compilerOptions"
Cohesion: 0.11
Nodes (17): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+9 more)

### Community 30 - "stage5_baseline_comparison.py"
Cohesion: 0.07
Nodes (19): HypothesisItem, InvestigationLoopState, InvestigationModelResponse, InvestigationResult, ModelAdapterProtocol, ProbeSelection, ProposedPlan, AbstentionReason (+11 more)

### Community 31 - "models.py"
Cohesion: 0.12
Nodes (19): ApprovalRequest, Base, ConsentEpoch, ExperienceCapsule, FailureCase, ImprovementProposal, LessonUsage, Outbox (+11 more)

### Community 32 - "stage4_postgres_restore_drill.py"
Cohesion: 0.17
Nodes (10): _create_isolated_db(), _drop_isolated_db(), inspect_database_fingerprint(), _norm_pg_expr(), _make_async_engine(), _pg_env(), run_drill(), _upgrade_isolated_db_to_head() (+2 more)

### Community 33 - "InvestigationStep"
Cohesion: 0.15
Nodes (9): EvidenceLedger, InvestigationStep, ToolCall, ToolCatalog, test_stage6_full_operator_control_plane_lifecycle(), test_p1_workbench_abstention_detection(), test_p1_workbench_missing_incident_returns_404(), test_p1_workbench_snapshot_empty_and_populated() (+1 more)

### Community 34 - ".admit_incident"
Cohesion: 0.36
Nodes (10): IncidentRepository, test_adversarial_cas_concurrent_dispute_resolution(), test_adversarial_consent_race_concurrent_reads(), test_adversarial_cross_tenant_isolation_gate(), test_adversarial_delayed_relapse_injection(), test_adversarial_model_self_grading_evasion_blocked(), test_adversarial_negative_control_zero_false_success_tolerance(), test_adversarial_redaction_error_quarantines_capsule() (+2 more)

### Community 35 - "stage7.ts"
Cohesion: 0.14
Nodes (13): ConfidenceBand, ConsentStatusResponse, ConsentUpdateRequest, ExperienceCapsuleResponse, ExperienceClassification, ExperienceListResponse, ExperienceState, ObservationResponse (+5 more)

### Community 37 - "Incident"
Cohesion: 0.07
Nodes (17): ContextManifest, EvidenceClassification, EvidenceRecord, EvidenceValidity, ContextManifestBuilder, EvidenceLedgerService, test_adversarial_cross_tenant_evidence_tampering_is_rejected(), test_adversarial_indirect_prompt_injection_is_inert() (+9 more)

### Community 38 - "WorkflowIncidentInput"
Cohesion: 0.14
Nodes (11): InvestigationObservation, InvestigationResult, WorkflowIncidentInput, FakeInvestigator, test_cross_tenant_read_and_cancel_denied_with_404(), test_fake_investigator_cannot_mutate_or_accept_command_injection(), test_forged_tenant_id_or_workflow_id_in_body_rejected_with_422(), test_log_injection_and_secret_redaction_never_expose_env_values() (+3 more)

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
Cohesion: 0.14
Nodes (15): create_improvement_draft(), DraftProposalRequest, FailureCaseResponse, FailureClusterResponse, get_authenticated_tenant_id(), get_failure_cluster(), get_session_factory(), ImprovementProposalResponse (+7 more)

### Community 47 - "test_stage7_e1_e2_lifecycle.py"
Cohesion: 0.08
Nodes (7): ConsentStatusResponse, ExperienceAuditAction, ConsentManager, RetentionWorker, ExperienceAudit, test_stage7_complete_e1_e2_lifecycle_and_view_integration(), test_e1_consent_lifecycle_and_epoch_advance()

### Community 51 - "conformance.py"
Cohesion: 0.06
Nodes (30): AdapterConformanceError, AdapterDigestMismatchError, AdapterEnablementExpiredError, AdapterNotConformantError, AdapterQuarantinedError, ConformanceRecordSnapshot, ConformanceReport, ConformanceRunner (+22 more)

### Community 68 - "recovery.py"
Cohesion: 0.07
Nodes (17): DegradedRecoveryGate, DegradedRecoveryModeError, ReservationState, ResourceEpoch, ResourceReservation, CrossStoreRecoveryCoordinator, RecoveryReport, test_divergent_epoch_fences_db_behind_and_target_ahead() (+9 more)

### Community 69 - "App.tsx"
Cohesion: 0.20
Nodes (12): App(), CANCELLABLE_STATES, generateIdempotencyKey(), getInitialIncidentId(), getInitialTenantId(), IncidentResponse, IncidentStatus, InvestigationObservation (+4 more)

### Community 70 - "stage6.ts"
Cohesion: 0.16
Nodes (12): Props, TraceExplorerView(), AbstentionDetail, ApprovalState, BudgetHeadroom, EvidenceItem, HypothesisItem, InvestigationStepItem (+4 more)

### Community 71 - "main.py"
Cohesion: 0.12
Nodes (10): create_app(), correlation_and_size_guard(), readyz(), validation_exception_handler(), _ensure_app_engine(), lifespan(), Settings, redact_value() (+2 more)

### Community 72 - "lessons.py"
Cohesion: 0.15
Nodes (12): draft_lesson(), DraftLessonRequest, get_authenticated_tenant_id(), get_session_factory(), list_lessons(), OperationalLessonResponse, revoke_lesson(), RevokeLessonRequest (+4 more)

### Community 73 - "test_e1_experience_capture.py"
Cohesion: 0.12
Nodes (13): CapsuleRedactionError, ExperienceClassification, ExperienceState, ExperienceProjector, ExperienceRedactor, _to_response(), test_adversarial_malformed_redaction_inputs(), test_e1_api_routes() (+5 more)

### Community 74 - "WorkbenchView.tsx"
Cohesion: 0.32
Nodes (6): ApprovalCockpitView(), Props, Props, WorkbenchView(), ApprovalResponse, WorkbenchSnapshotResponse

### Community 75 - "react"
Cohesion: 0.33
Nodes (5): dependencies, react@^19.3.1, react-dom@^19.3.1, react, react-dom

### Community 76 - "trace/service.py"
Cohesion: 0.12
Nodes (9): get_incident_trace(), IncidentNotFoundError, TraceEdge, TraceGraphResponse, TraceNode, TraceNodeKind, TraceRelation, _detect_cycles() (+1 more)

### Community 77 - "scripts"
Cohesion: 0.40
Nodes (5): scripts, build, dev, lint, preview

### Community 79 - "test_e4_lesson_registry.py"
Cohesion: 0.20
Nodes (10): LessonType, LessonRegistry, _setup_tenant_consent_and_outcome(), test_cross_tenant_lesson_isolation(), test_draft_lesson_requires_settled_outcome_and_consent(), test_lesson_usage_lineage_recorded(), test_synchronous_invalidation_on_consent_revocation(), test_synchronous_invalidation_on_outcome_supersession() (+2 more)

### Community 82 - "LessonRetriever"
Cohesion: 0.10
Nodes (7): LessonApplicabilityPredicate, LessonCitation, sanitize_lesson_summary_for_llm(), LessonRetriever, test_adversarial_consent_epoch_invalidation_race(), test_adversarial_prompt_injection_sanitization_and_isolation(), test_sanitize_lesson_summary_strips_code_and_injections()

### Community 83 - "workbench/service.py"
Cohesion: 0.20
Nodes (9): AbstentionDetail, BudgetHeadroom, EvidenceItem, HypothesisItem, InvestigationStepItem, ToolCallItem, WorkbenchSnapshotResponse, BudgetLedger (+1 more)

### Community 84 - "lesson_registry.py"
Cohesion: 0.13
Nodes (6): LessonConfidenceBand, LessonConflictError, LessonLifecycle, LessonNotFoundError, LessonValidationError, OperationalLesson

### Community 88 - "failure.py"
Cohesion: 0.15
Nodes (7): BudgetTuningDiff, FailureClusterStatus, PromptRefinementDiff, ProposalConflictError, ProposalLifecycleError, ProposalNotFoundError, ToolDescriptionDiff

### Community 89 - "ProposalType"
Cohesion: 0.17
Nodes (9): ProposalType, ProposalValidator, TargetComponent, test_proposal_validator_rejects_extra_fields_in_diff(), test_proposal_validator_rejects_forbidden_component(), test_proposal_validator_rejects_python_code_smuggling(), test_proposal_validator_rejects_sql_smuggling(), test_proposal_validator_valid_budget_tuning() (+1 more)

### Community 90 - "stage8.ts"
Cohesion: 0.14
Nodes (13): DivergentStepCategory, FailureCaseResponse, FailureClassificationStatus, FailureClusterResponse, FailureClusterStatus, ImprovementProposalResponse, LessonCitation, LessonLifecycle (+5 more)

### Community 91 - "EffectReceipt"
Cohesion: 0.37
Nodes (5): EffectReceipt, test_p3_trace_graph_cross_incident_receipt_isolation_and_digest_matching(), test_p3_trace_graph_dag_projection_and_acyclicity(), test_p3_trace_graph_missing_incident_returns_404(), test_p3_trace_graph_missing_link_annotations()

### Community 93 - "test_stage8_tables_and_columns_exist"
Cohesion: 0.25
Nodes (3): test_historical_data_preservation_opsblaze(), test_historical_data_preservation_opsblaze_test(), test_stage8_tables_and_columns_exist()

## Knowledge Gaps
- **2 isolated node(s):** `RFC-6455`, `opsblaze`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 951 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **35 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `IncidentRepository` connect `.admit_incident` to `test_b1_api_idempotency.py`, `test_stage8_omega_adversarial.py`, `models.py`, `test_b3_effect_state_machine.py`, `test_b1_state_machine.py`, `fencing.py`, `OutboxDispatcher`, `test_e3_failure_taxonomy.py`, `test_4_and_5_failure_before_and_during_commit_rolls_back_atomically`, `IncidentStatus`, `test_stage5_investigation_workflow.py`, `incidents.py`, `stage3_perf_stress.py`, `OutcomeService`, `activities.py`, `test_stage4_temporal_persistence_recovery.py`, `DurableBudgetManager`, `.admit_incident`, `models.py`, `InvestigationStep`, `test_stage7_e1_e2_lifecycle.py`, `conformance.py`, `stream_incident_events`, `recovery.py`, `main.py`, `test_e1_experience_capture.py`, `trace/service.py`, `test_e4_lesson_registry.py`, `sqlalchemy_ext_asyncio`, `workbench/service.py`, `EffectReceipt`, `test_stage8_e3_e4_lifecycle.py`?**
  _High betweenness centrality (0.125) - this node is a cross-community bridge._
- **Are the 22 inferred relationships involving `IncidentRepository` (e.g. with `cancel_incident()` and `create_incident()`) actually correct?**
  _`IncidentRepository` has 22 INFERRED edges - model-reasoned connections that need verification._
- **What connects `RFC-6455`, `opsblaze` to the rest of the system?**
  _2 weakly-connected nodes found - possible missing edges._
- **Should `server.cjs` be split into smaller, more focused modules?**
  _Cohesion score 0.05034965034965035 - nodes in this community are weakly interconnected._
- **Why does `Incident` connect `DurableBudgetManager` to `test_b1_api_idempotency.py`, `models.py`, `fencing.py`, `OutboxDispatcher`, `test_e3_failure_taxonomy.py`, `test_4_and_5_failure_before_and_during_commit_rolls_back_atomically`, `incidents.py`, `stage3_perf_stress.py`, `OutcomeService`, `activities.py`, `gateway.py`, `.admit_incident`, `stage5_baseline_comparison.py`, `models.py`, `.admit_incident`, `Incident`, `recovery.py`, `test_e1_experience_capture.py`, `test_e4_lesson_registry.py`, `sqlalchemy_ext_asyncio`?**
  _High betweenness centrality (0.055) - this node is a cross-community bridge._
- **Are the 47 inferred relationships involving `IncidentStatus` (e.g. with `_attach_investigation_if_applicable()` and `cancel_incident()`) actually correct?**
  _`IncidentStatus` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Should `package.json` be split into smaller, more focused modules?**
  _Cohesion score 0.13333333333333333 - nodes in this community are weakly interconnected._