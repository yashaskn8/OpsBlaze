/**
 * TypeScript type definitions for OpsBlaze Feature E3 (Failure Taxonomy & Proposals)
 * and Feature E4 (Validated Operational Lesson Registry).
 */

export type FailureClassificationStatus =
  | 'DETECTED'
  | 'CLASSIFIED'
  | 'UNCERTAIN'
  | 'CLUSTERED'
  | 'IGNORED'

export type FailureClusterStatus = 'ACTIVE' | 'QUARANTINED' | 'RESOLVED' | 'MERGED'

export type DivergentStepCategory =
  | 'ADAPTER_UNAVAILABLE'
  | 'PROBE_TIMEOUT'
  | 'FENCE_REJECTED'
  | 'PLAN_VALIDATION_FAILED'
  | 'READBACK_MISMATCH'
  | 'BUDGET_EXCEEDED'
  | 'AUTHORIZATION_DENIED'
  | 'EVIDENCE_STALE'
  | 'EXECUTION_ERROR'
  | 'UNKNOWN_CATEGORY'

export type ProposalType =
  | 'PROMPT_REFINEMENT'
  | 'CONTEXT_WINDOW_BUDGET'
  | 'TOOL_DESCRIPTION'

export type ProposalStatus =
  | 'DRAFT'
  | 'PROPOSED'
  | 'REVIEWED'
  | 'EVALUATION_READY'
  | 'REJECTED'

export type ReviewDecision = 'APPROVED' | 'REJECTED' | 'CHANGES_REQUESTED'

export interface FailureCaseResponse {
  id: string
  tenant_id: string
  incident_id: string
  outcome_revision_id: string
  cluster_id: string | null
  failure_origin_code: string
  subsystem_version: string
  first_divergent_step_category: DivergentStepCategory
  first_divergent_step: string | null
  classification_status: FailureClassificationStatus
  classification_version: string
  evidence_digest: string
  created_at: string
}

export interface FailureClusterResponse {
  id: string
  tenant_id: string
  signature_hash: string
  failure_origin_code: string
  subsystem_version: string
  first_divergent_step_category: DivergentStepCategory
  cluster_status: FailureClusterStatus
  case_count: number
  exemplar_case_id: string | null
  version: number
  quarantine_reason: string | null
  created_at: string
}

export interface ImprovementProposalResponse {
  id: string
  tenant_id: string
  cluster_id: string
  proposal_type: ProposalType
  base_policy_version: string
  target_component: string
  structured_diff: Record<string, unknown>
  hypothesis: string
  expected_benefit: string
  risk_assessment: string
  evaluation_plan: string
  status: ProposalStatus
  version: number
  drafter_kind: 'MODEL' | 'HUMAN'
  drafter_id: string
  reviewer_id: string | null
  reviewer_role: string | null
  review_decision: ReviewDecision | null
  review_comments: string | null
  rejection_code: string | null
  rejection_reason: string | null
  decided_at: string | null
  created_at: string
}

export type LessonType = 'VERIFIED_RECOVERY' | 'FAILURE_PATTERN' | 'COUNTEREXAMPLE'

export type LessonLifecycle =
  | 'DRAFT'
  | 'SOURCE_CHECKED'
  | 'VALIDATED'
  | 'ACTIVE'
  | 'EXPIRED'
  | 'QUARANTINED'
  | 'REVOKED'

export interface OperationalLessonResponse {
  id: string
  tenant_id: string
  lesson_id: string
  version: number
  lesson_type: LessonType
  title: string
  redacted_summary: string
  applicability_predicate: Record<string, unknown>
  consent_epoch: number
  confidence_band: 'HIGH' | 'MEDIUM' | 'LOW'
  lifecycle_state: LessonLifecycle
  valid_from: string
  valid_until: string
  created_by: string
  validated_by: string | null
  validated_at: string | null
  quarantine_reason: string | null
  source_outcome_revision_id: string
  source_capsule_id: string | null
  source_cluster_id: string | null
  proposal_id: string | null
  created_at: string
}

export interface LessonCitation {
  lesson_id: string
  version: number
  lesson_type: LessonType
  title: string
  sanitized_summary: string
  confidence_band: 'HIGH' | 'MEDIUM' | 'LOW'
  source_outcome_revision_id: string
  source_digest: string
  retrieval_digest: string
}
