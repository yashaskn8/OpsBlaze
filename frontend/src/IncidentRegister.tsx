import type { Incident } from "./api";

type RegisterProps = {
  items: Incident[] | null;
  incidentId: string | undefined;
  events: { id: number; kind: string; projection_version: number }[];
  error: string | undefined;
  loading: boolean;
  canLoad: boolean;
  loadRecent: () => void;
  chooseIncident: (incident: Incident) => void;
};

export function IncidentRegister({ items, incidentId, events, error, loading, canLoad, loadRecent, chooseIncident }: RegisterProps) {
  return (
    <section id="recent-incidents" tabIndex={-1} className="surface recent-register" aria-labelledby="recent-incidents-title">
      <div className="history-header">
        <h2 id="recent-incidents-title">Recent incidents</h2>
        <button type="button" className="secondary" disabled={!canLoad || loading} onClick={loadRecent}>
          {loading ? "Loading incidents…" : "Load from server"}
        </button>
      </div>
      {error && <p role="alert" className="error">{error}</p>}
      {loading && <p role="status" className="muted">Loading the recent incident register…</p>}
      {!items?.length ? !loading && !error && <p className="muted">
        {items ? "No recent incidents for this tenant." : "Load the latest incidents for this tenant."}
      </p> :
        <table className="register-table">
          <caption className="sr-only">Recent incidents in the selected tenant</caption>
          <thead><tr><th scope="col">Target</th><th scope="col">State</th></tr></thead>
          <tbody>{items.map(item => (
            <tr key={item.id} data-selected={incidentId === item.id || undefined}>
              <td><button type="button" className="recent-item"
                aria-current={incidentId === item.id ? "true" : undefined}
                onClick={() => chooseIncident(item)}>
                <strong>{item.target_id}</strong><code>{item.id}</code>
              </button></td>
              <td>
                {item.status_projection.toLowerCase().replaceAll("_", " ")}
                <small className="muted">Version {item.status_version}</small>
              </td>
            </tr>
          ))}</tbody>
        </table>
      }
      {incidentId && <div className="audit">
        <h3>Durable event history</h3>
        {events.length === 0 ? <p className="muted">No events loaded yet.</p> :
          events.map(event => <div className="audit-item" key={event.id}>
            <strong>#{event.id}: {event.kind.replaceAll("_", " ")}</strong>
            <span>Version {event.projection_version}</span>
          </div>)
        }
      </div>}
    </section>
  );
}
