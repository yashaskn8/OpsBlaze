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
