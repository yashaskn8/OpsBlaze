/**
 * TypeScript type definitions for OpsBlaze Feature E1 (Experience Capsule) and E2 (Outcome Adjudication).
 */

export type ExperienceState = 'CAPTURED' | 'ELIGIBLE' | 'QUARANTINED' | 'TOMBSTONED' | 'PURGED'

export type ExperienceClassification =
  | 'INVESTIGATION_SUCCESS'
  | 'INVESTIGATION_FAILURE'
  | 'ABSTENTION'
  | 'CONSEQUENTIAL_ACTION'
  | 'UNKNOWN'

export interface ExperienceCapsuleResponse {
  id: string
  tenant_id: string
  incident_id: string
  step_id: string | null
  schema_version: string
  consent_epoch: number
  policy_bundle_version: string
  model_id: string
  model_version: string
  evidence_manifest_sha256: string
  effect_receipt_id: string | null
  outcome_id: string | null
  outcome_revision_no: number | null
  redaction_version: string
  redacted_digest: string
  summary_markdown: string | null
  classification: ExperienceClassification
  lifecycle_state: ExperienceState
  occurred_at: string
  expires_at: string
  created_at: string
}

export interface ExperienceListResponse {
  items: ExperienceCapsuleResponse[]
  next_cursor: string | null
  total_eligible: number
}

export interface ConsentStatusResponse {
  tenant_id: string
  current_epoch: number
  reuse_allowed: boolean
  updated_at: string
  revoked_at: string | null
}

export interface ConsentUpdateRequest {
  reuse_allowed: boolean
  reason: string
  actor_id: string
  actor_role: string
}

export type OutcomeLabel =
  | 'PENDING'
  | 'OBSERVED'
  | 'VERIFIED_SUCCESS'
  | 'VERIFIED_FAILURE'
  | 'DISPUTED'
  | 'REVISED'
  | 'UNKNOWN'

export type ConfidenceBand = 'HIGH' | 'MEDIUM' | 'LOW' | 'UNKNOWN'

export type SourceKind =
  | 'INDEPENDENT_WORKLOAD_CHECK'
  | 'TARGET_READBACK'
  | 'DELAYED_MONITOR'
  | 'OPERATOR_FEEDBACK'
  | 'ADAPTER_ACK'
  | 'MODEL_ANNOTATION'

export interface ObservationResponse {
  id: string
  tenant_id: string
  incident_id: string
  source_kind: SourceKind
  source_reference: string
  rule_version: string
  result_payload: Record<string, unknown>
  source_digest: string
  idempotency_key: string
  observed_at: string
  created_at: string
}

export interface OutcomeRevisionResponse {
  id: string
  tenant_id: string
  incident_id: string
  revision_no: number
  rule_version: string
  label: OutcomeLabel
  confidence_band: ConfidenceBand
  dispute_reason: string | null
  prior_revision_id: string | null
  reviewer_id: string | null
  reviewer_role: string | null
  supporting_observation_ids: string[]
  adjudication_metadata: Record<string, unknown>
  decided_at: string
  created_at: string
}

export interface OutcomeSummaryResponse {
  incident_id: string
  tenant_id: string
  current_revision: OutcomeRevisionResponse | null
  is_disputed: boolean
  total_observations: number
  observations: ObservationResponse[]
}

export interface OperatorFeedbackRequest {
  label: OutcomeLabel
  reason: string
  actor_id: string
  actor_role: string
  expected_revision_no: number
}
