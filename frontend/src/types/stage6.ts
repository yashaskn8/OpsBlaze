export type ApprovalState =
  | 'PENDING'
  | 'APPROVED'
  | 'REJECTED'
  | 'REVOKED'
  | 'INVALIDATED'
  | 'EXPIRED'
  | 'DISPATCHING'
  | 'CONSUMED'

export interface ApprovalResponse {
  id: string
  tenant_id: string
  incident_id: string
  step_id: string | null
  plan_id: string
  canonical_plan_digest: string
  action_digest: string
  action_payload: Record<string, unknown>
  target_id: string
  expected_resource_version: string
  policy_version: string
  state: ApprovalState
  reviewer_id: string | null
  reviewer_role: string | null
  expires_at: string
  decision_at: string | null
  reason: string | null
  dispatch_lease_id: string | null
  dispatch_lease_expires_at: string | null
  created_at: string
  updated_at: string
}

export interface HypothesisItem {
  hypothesis_id: string
  title: string
  confidence: number
  status: string
  rationale: string
  rank: number
}

export interface EvidenceItem {
  id: string
  tool_call_id: string | null
  source: string
  source_version: string
  target_id: string
  target_version: string
  observed_at: string
  captured_at: string
  sha256_digest: string
  raw_payload_redacted: Record<string, unknown>
  classification: string
  validity_status: string
}

export interface InvestigationStepItem {
  id: string
  step_no: number
  loop_state: string
  prompt_digest: string
  token_count: number
  abstention_reason: string | null
  created_at: string
}

export interface ToolCallItem {
  id: string
  step_no: number
  tool_id: string
  tool_version: string
  target_id: string
  status: string
  duration_ms: number
  args_digest: string
  output_digest: string | null
  error_detail: string | null
  created_at: string
}

export interface BudgetHeadroom {
  reserved_tokens: number
  settled_tokens: number
  reserved_probes: number
  settled_probes: number
  reserved_cost_micros: number
  settled_cost_micros: number
  max_steps: number
  current_step: number
}

export interface AbstentionDetail {
  abstained: boolean
  reason: string | null
  confidence: number
  missing_evidence_types: string[]
}

export interface WorkbenchSnapshotResponse {
  incident: {
    id: string
    tenant_id: string
    client_key: string
    target_id: string
    request_sha256: string
    status: string
    status_version: number
    workflow_id: string
    created_at: string
    updated_at: string
    outbox_delivered: boolean
    outbox_attempts: number
    projection_lag_ms: number
    projection_stale: boolean
  }
  hypotheses: HypothesisItem[]
  evidence: EvidenceItem[]
  steps: InvestigationStepItem[]
  tool_calls: ToolCallItem[]
  budget: BudgetHeadroom
  active_approval: ApprovalResponse | null
  abstention: AbstentionDetail
  projection_lag_ms: number
  etag: string
  outcome_label?: string | null
  outcome_revision_no?: number | null
  outcome_is_disputed?: boolean
}

export interface TraceNode {
  id: string
  kind: 'INCIDENT' | 'STEP' | 'TOOL_CALL' | 'EVIDENCE' | 'PLAN' | 'APPROVAL' | 'EFFECT_RECEIPT' | 'OUTCOME' | 'EXPERIENCE'
  label: string
  timestamp: string
  status: string
  metadata: Record<string, unknown>
  warning: string | null
}

export interface TraceEdge {
  source: string
  target: string
  relation: string
}

export interface TraceGraphResponse {
  incident_id: string
  tenant_id: string
  root_node_id: string
  nodes: TraceNode[]
  edges: TraceEdge[]
  missing_links: string[]
  has_cycles: boolean
}
