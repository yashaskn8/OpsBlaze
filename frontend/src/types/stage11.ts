/**
 * Feature E6 — Policy Release Control & Automated Rollback Types
 */

export type ReleaseState =
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'SHADOW'
  | 'CANARY'
  | 'ACTIVE'
  | 'ROLLED_BACK'
  | 'REVOKED'

export type BundleStatus = 'DRAFT' | 'SEALED' | 'ARCHIVED'

export type CanaryArm = 'INCUMBENT' | 'CANDIDATE'

export interface PolicyBundle {
  id: string
  tenant_id: string
  version: number
  bundle_name: string
  bundle_sha256: string
  signature: string
  signature_key_id: string
  base_policy_version: string
  target_component: string
  proposal_diff: Record<string, unknown> | null
  prompt_templates: Record<string, unknown> | null
  active_lesson_ids: string[] | null
  model_id: string
  model_version: string
  tool_catalog_version: string
  a2_grant_version: string
  source_manifest_id: string | null
  source_report_id: string | null
  source_provenance_digest: string
  status: BundleStatus
  created_by: string
  created_at: string
  sealed_at: string | null
}

export interface PolicyRelease {
  id: string
  tenant_id: string
  version: number
  target_component: string
  bundle_id: string
  incumbent_bundle_id: string
  state: ReleaseState
  canary_percentage: number
  rollout_salt: string
  drafter_id: string
  reviewer_id: string | null
  reviewer_role: string | null
  decision_comments: string | null
  kill_switch_active: boolean
  kill_switch_triggered_by: string | null
  kill_switch_triggered_at: string | null
  rollback_reason: string | null
  created_at: string
  updated_at: string
  approved_at: string | null
  activated_at: string | null
  rolled_back_at: string | null
}

export interface ReleaseAuditEvent {
  id: string
  tenant_id: string
  release_id: string
  event_type: string
  from_state: string | null
  to_state: string
  actor_id: string
  actor_role: string
  metadata_redacted: Record<string, unknown>
  created_at: string
}

export interface CanaryMetrics {
  release_id: string
  target_component: string
  state: ReleaseState
  canary_percentage: number
  incumbent_sample_size: number
  candidate_sample_size: number
  incumbent_error_rate: number
  candidate_error_rate: number
  relative_risk: number | null
  invariant_breach_count: number
  recommendation: string
}

export interface PolicyReleaseCreateRequest {
  target_component: string
  bundle_id: string
  incumbent_bundle_id: string
  rollout_salt?: string
}

export interface ReleaseTransitionRequest {
  expected_version: number
  target_state: ReleaseState
  canary_percentage?: number
  comments?: string
}

export interface ReleaseReviewRequest {
  expected_version: number
  decision: 'APPROVED' | 'REJECTED'
  decision_comments?: string
}

export interface RollbackRequest {
  expected_version: number
  rollback_reason: string
}

export interface KillSwitchRequest {
  expected_version: number
  justification: string
}
