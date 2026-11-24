// Read-only operations HTTP contracts; auth headers belong to the caller.
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

async function readJson<T>(path: string, headers: Record<string, string>): Promise<T> {
  const response = await fetch(path, { headers });
  const body: unknown = await response.json();
  if (!response.ok) throw new Error(detail(body));
  return body as T;
}

export function fetchQueueHealth(headers: Record<string, string>): Promise<QueueHealth> {
  return readJson<QueueHealth>("/v1/operations/queues", headers);
}

export function fetchDeadLetters(
  headers: Record<string, string>, cursor: number,
): Promise<ExperienceDeadLetterPage> {
  const params = new URLSearchParams({ cursor: String(cursor), limit: "25" });
  return readJson<ExperienceDeadLetterPage>(
    "/v1/operations/dead-letters?" + params.toString(), headers,
  );
}
