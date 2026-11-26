// Read-only operations HTTP contracts; auth headers belong to the caller.
export type Incident = {
  id: string;
  target_id: string;
  status_projection: string;
  status_version: number;
  created_at: string;
};

export type Investigation = {
  incident: Incident;
  probe_budget: { used: number; limit: number; remaining: number };
  steps: {
    step_no: number;
    decision: string;
    probe_id: string;
    hypotheses: string[];
    incomplete_evidence: boolean;
    evidence: { id: string; source: string; validity: string; freshness?: string; redacted_excerpt: string }[];
  }[];
  steps_truncated: boolean;
  latest_assessment: { reason: string; outcome: string; step_no: number } | null;
  advisory_fallback_reason: string | null;
  latest_effect_state: string | null;
  recovery: {
    latest_outcome_revision: number | null;
    latest_outcome_label: string | null;
    latest_outcome_id: string | null;
    latest_outcome_witness_source: string | null;
    latest_outcome_recorded_at: string | null;
    latest_outcome_observed_version: number | null;
    admission_to_recorded_witness_ms: number | null;
  } | null;
};

export type QueueHealth = {
  pending_outbox: number;
  experience_dead_letters_unreviewed: number;
  active_incidents: number;
  unresolved_effects: number;
  aged_unresolved_effects: number;
  pending_approvals: number;
  aged_pending_approvals: number;
  oldest_pending_approval_seconds: number;
  pending_approval_age_p95_seconds: number;
  oldest_pending_seconds: number;
  oldest_active_phase_seconds: number;
  pending_age_p95_seconds: number;
  aged_pending_outbox: number;
  aged_active_incidents: number;
  max_pending_attempts: number;
  stuck_after_seconds: number;
};

export type ExperienceDeadLetter = {
  id: number;
  incident_id: string;
  attempts: number;
  code: "INVALID_PAYLOAD" | "RETRY_EXHAUSTED";
  dead_lettered_at: string;
  reviewed: boolean;
  redriven_to: number | null;
};

export type ExperienceDeadLetterPage = {
  records: ExperienceDeadLetter[];
  next_cursor: number | null;
  metadata_only: boolean;
};

export type LineageDiagnostics = {
  missing_edge_count: number;
  context_loss_count: number;
  projection_lag_seconds: number | null;
  projection_status: string;
};

export type LineageRecordDetail = {
  id: string;
  kind: string;
  record_id: string;
  incident_id: string;
  status: string;
  authority_kind:
    | "OBSERVED_EVIDENCE"
    | "MODEL_OR_INVESTIGATION_CLAIM"
    | "PROPOSED_ACTION"
    | "HUMAN_AUTHORIZATION"
    | "EFFECT_LEDGER"
    | "INDEPENDENT_OUTCOME"
    | "DERIVED_LEARNING";
  verified_outcome: boolean;
  unresolved_or_failed: boolean;
  details: Record<string, unknown>;
  trace_refs: { node_id: string; trace_id: string; span_id: string; collector_verified: false }[];
};

export type EvidenceItem = {
  id: string;
  source: string;
  validity: string;
  freshness?: string;
  supersedes_evidence_id?: string | null;
  redacted_excerpt: string;
};

export type EvidencePage = {
  items: EvidenceItem[];
  next_cursor: string | null;
};

export function detail(value: unknown): string {
  if (value && typeof value === "object" && "detail" in value) {
    const message = (value as { detail: unknown }).detail;
    return typeof message === "string" ? message : JSON.stringify(message);
  }
  return "Request failed";
}

export async function finishReview(
  response: Response, refresh: () => Promise<void>,
): Promise<void> {
  if (response.ok) {
    await refresh();
    return;
  }
  const body: unknown = await response.json();
  if (response.status === 409 || response.status === 403) await refresh();
  throw new Error(detail(body));
}

async function readJson<T>(path: string, headers: Record<string, string>): Promise<T> {
  const response = await fetch(path, { headers });
  const body: unknown = await response.json();
  if (!response.ok) throw new Error(detail(body));
  return body as T;
}

export function fetchQueueHealth(headers: Record<string, string>): Promise<QueueHealth> {
  return readJson<QueueHealth>("/v1/operations/queues", headers);
}

export async function fetchInvestigation(
  incidentId: string, headers: Record<string, string>, signal: AbortSignal,
): Promise<Investigation> {
  const path = `/v1/incidents/${encodeURIComponent(incidentId)}/investigation`;
  const response = await fetch(path, { headers, signal });
  const body: unknown = await response.json();
  if (!response.ok) throw new Error(detail(body));
  const report = body as Investigation;
  if (report?.incident?.id !== incidentId) throw new Error("Investigation did not match the selected incident");
  return report;
}

export function fetchDeadLetters(
  headers: Record<string, string>, cursor: number,
): Promise<ExperienceDeadLetterPage> {
  const params = new URLSearchParams({ cursor: String(cursor), limit: "25" });
  return readJson<ExperienceDeadLetterPage>(
    "/v1/operations/dead-letters?" + params.toString(), headers,
  );
}

export async function fetchLineageRecordDetail(
  incidentId: string,
  kind: string,
  recordId: string,
  headers: Record<string, string>,
): Promise<LineageRecordDetail> {
  const path =
    `/v1/incidents/${encodeURIComponent(incidentId)}/lineage/record/`
    + `${encodeURIComponent(kind)}/${encodeURIComponent(recordId)}`;
  const record = await readJson<LineageRecordDetail>(path, headers);
  if (record?.incident_id !== incidentId || record?.id !== `${kind}:${recordId}`) {
    throw new Error("Lineage record detail did not match the selected node");
  }
  return record;
}

export function fetchEvidencePage(
  incidentId: string,
  headers: Record<string, string>,
  cursor?: string | null,
  limit = 25,
): Promise<EvidencePage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  return readJson<EvidencePage>(
    `/v1/incidents/${encodeURIComponent(incidentId)}/evidence/page?` + params.toString(),
    headers,
  );
}

export type HandoffReasonCode =
  | "LOW_CONFIDENCE"
  | "STALE_EVIDENCE"
  | "UNRESOLVED_EFFECT"
  | "POLICY_BOUNDARY"
  | "OPERATOR_REQUESTED";

export async function handoffIncident(
  incidentId: string,
  version: number,
  reasonCode: HandoffReasonCode,
  note: string,
  headers: Record<string, string>,
): Promise<Incident & { reason_code: HandoffReasonCode; next_steps: string[]; evidence_ids: string[] }> {
  const response = await fetch(`/v1/incidents/${encodeURIComponent(incidentId)}/handoff`, {
    method: "POST",
    headers: {
      ...headers,
      "Content-Type": "application/json",
      "If-Match": String(version),
    },
    body: JSON.stringify({ reason_code: reasonCode, note }),
  });
  const body = (await response.json()) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
  }
  return body as Incident & { reason_code: HandoffReasonCode; next_steps: string[]; evidence_ids: string[] };
}
