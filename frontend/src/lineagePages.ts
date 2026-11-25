export type LineagePageIdentity = {
  incident_id: string;
  kind: string;
  next_cursor: string | null;
};

export type LineagePages<T extends LineagePageIdentity> = {
  incidentId: string;
  pages: Record<string, T>;
};

// Keep just the current additional page per kind, rather than an unbounded history.
export function replaceLineagePage<T extends LineagePageIdentity>(
  current: LineagePages<T>, page: T,
): LineagePages<T> {
  const pages = current.incidentId === page.incident_id ? current.pages : {};
  return { incidentId: page.incident_id, pages: { ...pages, [page.kind]: page } };
}

export type EvidenceProjectionStatus = "MATCHED" | "STALE_OR_MISSING" | "NOT_CHECKED_TRUNCATED" | "NOT_APPLICABLE";

export function evidenceProjectionWarning(statuses: readonly unknown[]): string | null {
  if (statuses.includes("STALE_OR_MISSING")) {
    return "Evidence-edge projection is stale or missing. Live source records are shown; cached links are not trusted.";
  }
  if (statuses.includes("NOT_CHECKED_TRUNCATED")) {
    return "Evidence-edge projection agreement was not checked for truncated data.";
  }
  if (statuses.some(status => status !== "MATCHED" && status !== "NOT_APPLICABLE")) {
    return "Evidence-edge projection status is unavailable.";
  }
  return null;
}
