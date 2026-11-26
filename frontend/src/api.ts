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
    evidence: { id: string; source: string; validity: string; redacted_excerpt: string }[];
  }[];
  steps_truncated: boolean;
  latest_assessment: { reason: string; outcome: string; step_no: number } | null;
  latest_effect_state: string | null;
  recovery: {
    latest_outcome_revision: number | null;
    latest_outcome_label: string | null;
    admission_to_recorded_witness_ms: number | null;
  } | null;
};

export type QueueHealth = {
  pending_outbox: number;
  experience_dead_letters_unreviewed: number;
  active_incidents: number;
  unresolved_effects: number;
  aged_unresolved_effects: number;
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
