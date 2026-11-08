/**
 * TypeScript type definitions for OpsBlaze Feature E5:
 * Candidate-to-Incumbent Regression Gate.
 */

export type ManifestLifecycleState =
  | 'REGISTERED'
  | 'FROZEN'
  | 'RUNNING'
  | 'ANALYZING'
  | 'COMPLETED'
  | 'INVALIDATED'
  | 'CANCELLED'

export type CandidateProvenanceType =
  | 'E3_PROPOSAL'
  | 'E4_LESSON_SET'
  | 'COMBINED_OFFLINE_STUDY'

export type TrialArm = 'INCUMBENT' | 'CANDIDATE'

export type TrialStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'INVALID'

export type TrialValidityState =
  | 'VALID'
  | 'INVALID_TIMEOUT'
  | 'INVALID_ORACLE_OUTAGE'
  | 'INVALID_FIXTURE_CORRUPT'

export type ReportStatus = 'PASS' | 'FAIL' | 'INVALID' | 'INCONCLUSIVE'

export type ReviewDecision = 'ACCEPTED' | 'REJECTED' | 'INCONCLUSIVE_ACKNOWLEDGED'

export interface PromptRefinementDiff {
  template_name: string
  section_name: string
  original_text: string
  proposed_text: string
}

export interface ContextBudgetDiff {
  component: string
  current_tokens: number
  proposed_tokens: number
}

export interface ToolDescriptionDiff {
  tool_name: string
  original_description: string
  proposed_description: string
}

export interface CandidateBundle {
  base_policy_version: string
  target_component: string
  proposal_diff?: PromptRefinementDiff | ContextBudgetDiff | ToolDescriptionDiff | null
  active_lesson_ids?: string[]
  active_lesson_epoch?: number | null
}

export interface IncumbentBundle {
  policy_version: string
  prompt_templates?: Record<string, string>
  budget_bounds?: Record<string, number>
  tool_catalog_version: string
}

export interface MetricSpec {
  non_inferiority_margin?: number
  cost_ceiling_multiplier?: number
  alpha?: number
  min_sample_size?: number
  max_invalid_pairs_ratio?: number
}

export interface GateVerdict {
  gate_name: string
  status: 'PASS' | 'FAIL' | 'INCONCLUSIVE' | 'SKIPPED'
  threshold: number | string
  observed_value?: number | string | null
  details?: Record<string, unknown>
}

export interface CandidateManifestResponse {
  id: string
  tenant_id: string
  title: string
  manifest_sha256: string
  incumbent_digest: string
  candidate_digest: string
  candidate_provenance_type: CandidateProvenanceType
  source_proposal_id: string | null
  source_proposal_version: number | null
  source_lesson_ids: string[] | null
  model_id: string
  model_version: string
  tool_catalog_version: string
  a2_grant_version: string
  fixture_family: string
  fixture_set_digest: string
  holdout_set_digest: string
  oracle_version: string
  adjudication_rule_version: string
  metric_spec: MetricSpec
  primary_endpoint: string
  sample_size_plan: number
  random_seed: number
  budget_ceiling_micros: number
  max_tokens_ceiling: number
  lifecycle_state: ManifestLifecycleState
  version: number
  created_by: string
  created_at: string
  updated_at: string
  frozen_at: string | null
}

export interface CandidateTrialResponse {
  id: string
  tenant_id: string
  manifest_id: string
  pair_id: string
  arm: TrialArm
  fixture_id: string
  is_negative_control: boolean
  trial_status: TrialStatus
  outcome_label: string | null
  decision_correct: boolean | null
  probes_count: number
  tokens_consumed: number
  cost_micros: number
  duration_ms: number
  validity_state: TrialValidityState
  validity_reason: string | null
  unsafe_action_detected: boolean
  unsafe_action_count: number
  worker_id: string | null
  completed_at: string | null
}

export interface CandidateReportResponse {
  id: string
  tenant_id: string
  manifest_id: string
  report_status: ReportStatus
  promotion_eligible: boolean
  invalidation_reason: string | null
  total_pairs_planned: number
  total_pairs_completed: number
  invalid_pairs_count: number
  metrics_summary: Record<string, unknown>
  mcnemar_statistic: number | null
  mcnemar_p_value: number | null
  recovery_rate_incumbent: number
  recovery_rate_candidate: number
  recovery_delta: number
  recovery_ci_low: number
  recovery_ci_high: number
  cost_per_verified_incumbent: number
  cost_per_verified_candidate: number | null
  negative_control_pass_rate: number
  unsafe_action_count: number
  gate_verdicts: GateVerdict[]
  version: number
  reviewer_id: string | null
  reviewer_role: string | null
  review_decision: ReviewDecision | null
  review_comments: string | null
  source_eligibility_rechecked: boolean
  reviewed_at: string | null
  evaluated_at: string
}

export interface ManifestDetailResponse {
  manifest: CandidateManifestResponse
  trials_total: number
  trials_completed: number
  latest_report: CandidateReportResponse | null
}
