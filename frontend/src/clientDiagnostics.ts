// P1-16: strictly non-sensitive client stream and snapshot diagnostics.
export type ClientDiagnostics = {
  disconnects: number;
  eventGaps: number;
  clientErrors: number;
  lastSnapshotAtMs: number | null;
};

export function emptyDiagnostics(): ClientDiagnostics {
  return {
    disconnects: 0,
    eventGaps: 0,
    clientErrors: 0,
    lastSnapshotAtMs: null,
  };
}

export function snapshotAgeSeconds(
  diag: ClientDiagnostics,
  nowMs: number = Date.now(),
): number | null {
  if (diag.lastSnapshotAtMs === null) return null;
  return Math.max(0, Math.floor((nowMs - diag.lastSnapshotAtMs) / 1000));
}
