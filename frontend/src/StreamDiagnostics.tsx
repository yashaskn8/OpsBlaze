import { useEffect, useState } from "react";
import { snapshotAgeSeconds, type ClientDiagnostics } from "./clientDiagnostics";

export function StreamDiagnostics({ diagnostics }: { diagnostics: ClientDiagnostics }) {
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const { lastSnapshotAtMs } = diagnostics;

  useEffect(() => {
    if (!expanded || lastSnapshotAtMs === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [expanded, lastSnapshotAtMs]);

  const age = snapshotAgeSeconds(diagnostics, now);
  return <details className="stream-diagnostics" onToggle={event => {
    setExpanded(event.currentTarget.open);
    if (event.currentTarget.open) setNow(Date.now());
  }}>
    <summary>Client stream diagnostics</summary>
    <dl className="signal-strip">
      <div><dt>Disconnects</dt><dd>{diagnostics.disconnects}</dd></div>
      <div><dt>Event gaps</dt><dd>{diagnostics.eventGaps}</dd></div>
      <div><dt>Snapshot age</dt><dd>{age == null ? "Pending" : `${age} s`}</dd></div>
      <div><dt>Client errors</dt><dd>{diagnostics.clientErrors}</dd></div>
    </dl>
  </details>;
}
