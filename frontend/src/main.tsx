import { useEffect, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Incident = {
  id: string;
  target_id: string;
  status_projection: string;
  status_version: number;
  created_at: string;
};

type Plan = {
  id: string;
  action_id: string;
  target_id: string;
  status: string;
  version: number;
  plan_digest: string;
  expected_version: number;
  evidence_ids: string[];
};

type Evidence = {
  id: string;
  source: string;
  validity: string;
  redacted_excerpt: string;
};

type Effect = {
  id: string;
  state: string;
  adapter_id: string;
  observed_version: number | null;
};

type Lineage = {
  projection: "COMPLETE" | "PARTIAL_LINKS";
  trace_spans_available: boolean;
  nodes: { id: string; kind: string; status: string }[];
  edges: { from: string; to: string; relation: string }[];
  missing_refs: { source: string; missing_target: string; relation: string }[];
};

type UIEvent = {
  id: number;
  kind: string;
  projection_version: number;
  created_at: string;
};

type UIEventPage = { events: UIEvent[]; last_id: number };

function detail(value: unknown): string {
  if (value && typeof value === "object" && "detail" in value) {
    const message = (value as { detail: unknown }).detail;
    return typeof message === "string" ? message : JSON.stringify(message);
  }
  return "Request failed";
}

function App() {
  const [tenant, setTenant] = useState("demo");
  const [token, setToken] = useState("");
  const [target, setTarget] = useState("checkout");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [incident, setIncident] = useState<Incident | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [effects, setEffects] = useState<Effect[]>([]);
  const [lineage, setLineage] = useState<Lineage | null>(null);
  const [recent, setRecent] = useState<Incident[]>([]);
  const [events, setEvents] = useState<UIEvent[]>([]);
  const [connection, setConnection] = useState<"SYNCING" | "LIVE" | "STALE">("SYNCING");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const headers = () => ({ "X-Tenant-Id": tenant, "X-OpsBlaze-Token": token });

  async function admit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/incidents", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({ target_id: target.trim() })
      });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      setIncident(data as Incident);
      setPlan(null);
      setEvidence([]);
      setEffects([]);
      setLineage(null);
      setEvents([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    if (!incident) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/incidents/" + incident.id, { headers: headers() });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      setIncident(data as Incident);
      const getList = async <T,>(kind: string): Promise<T[]> => {
        const res = await fetch("/v1/incidents/" + incident.id + "/" + kind, {
          headers: headers()
        });
        const body: unknown = await res.json();
        if (!res.ok) throw new Error(detail(body));
        return body as T[];
      };
      const [plans, proof, receipts] = await Promise.all([
        getList<Plan>("plans"),
        getList<Evidence>("evidence"),
        getList<Effect>("effects")
      ]);
      setPlan(plans[0] ?? null);
      setEvidence(proof);
      setEffects(receipts);
      const lineageResponse = await fetch(
        "/v1/incidents/" + incident.id + "/lineage", { headers: headers() }
      );
      const lineageBody: unknown = await lineageResponse.json();
      if (!lineageResponse.ok) throw new Error(detail(lineageBody));
      setLineage(lineageBody as Lineage);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }



  async function loadRecent() {
    setError("");
    try {
      const response = await fetch("/v1/incidents", { headers: headers() });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      setRecent(data as Incident[]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not list incidents");
    }
  }

  function chooseIncident(item: Incident) {
    setIncident(item);
    setPlan(null);
    setEvidence([]);
    setEffects([]);
    setLineage(null);
    setEvents([]);
    setConnection("SYNCING");
  }

  useEffect(() => {
    const selectedId = incident?.id;
    if (!selectedId || !token || !tenant) return;
    let disposed = false;
    let afterId = 0;
    setConnection("SYNCING");

    async function pollEvents() {
      try {
        const response = await fetch(
          "/v1/incidents/" + selectedId + "/events?after_id=" + afterId,
          { headers: { "X-Tenant-Id": tenant, "X-OpsBlaze-Token": token } }
        );
        const data: unknown = await response.json();
        if (!response.ok) throw new Error(detail(data));
        if (disposed) return;
        const page = data as UIEventPage;
        afterId = page.last_id;
        if (page.events.length) {
          setEvents(previous => [...previous, ...page.events].slice(-20));
          // Notifications cannot authorize actions; fetch fresh server truth.
          const snapshot = await fetch(
            "/v1/incidents/" + selectedId,
            { headers: { "X-Tenant-Id": tenant, "X-OpsBlaze-Token": token } }
          );
          const current: unknown = await snapshot.json();
          if (!snapshot.ok) throw new Error(detail(current));
          if (!disposed) {
            const incoming = current as Incident;
            setIncident(previous =>
              previous && previous.id === incoming.id
                && previous.status_version > incoming.status_version
                  ? previous : incoming
            );
          }
        }
        if (!disposed) setConnection("LIVE");
      } catch {
        if (!disposed) setConnection("STALE");
      }
    }

    void pollEvents();
    const timer = window.setInterval(() => { void pollEvents(); }, 4000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [incident?.id, tenant, token]);

  async function decide(action: "cancel" | "acknowledge") {
    if (!incident) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/incidents/" + incident.id + "/" + action, {
        method: "POST",
        headers: { ...headers(), "If-Match": String(incident.status_version) }
      });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      setIncident(data as Incident);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }


  async function decidePlan(choice: "APPROVE" | "REJECT") {
    if (!plan || !incident) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/plans/" + plan.id + "/decisions", {
        method: "POST",
        headers: {
          ...headers(),
          "Content-Type": "application/json",
          "If-Match": String(plan.version)
        },
        body: JSON.stringify({ decision: choice })
      });
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      setPlan({ ...plan, status: choice === "APPROVE" ? "APPROVED" : "REJECTED",
                version: plan.version + 1 });
      // A refreshed snapshot is required before any other decision.
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <header><span className="mark">OB</span><div><strong>OpsBlaze</strong><small>Controlled incident recovery lab</small></div></header>
      <div className="intro"><p className="eyebrow">TITAN-20 / OPERATOR WORKBENCH</p><h1>Incident operations</h1><p>Admit incidents, inspect evidence, and review simulator-only recovery.</p></div>
      <div className="layout">
        <section>
          <h2>New incident</h2><p className="muted">Development only. Token stays in browser memory.</p>
          <form onSubmit={admit}>
            <label>Tenant ID<input required maxLength={64} value={tenant} onChange={e => setTenant(e.target.value)} /></label>
            <label>Local API token<input type="password" autoComplete="off" required value={token} onChange={e => setToken(e.target.value)} /></label>
            <label>Target ID<input required maxLength={256} value={target} onChange={e => setTarget(e.target.value)} /></label>
            <p className="key">Idempotency key <code>{key}</code></p>
            <div className="actions">
              <button type="submit" disabled={busy}>{busy ? "Working..." : "Admit incident"}</button>
              <button type="button" className="secondary" disabled={busy} onClick={() => { setKey(crypto.randomUUID()); setIncident(null); setPlan(null); setEvidence([]); setEffects([]); setLineage(null); setEvents([]); setConnection("SYNCING"); setError(""); }}>New request key</button>
            </div>
          </form>
        </section>
        <section aria-live="polite">
          <h2>Incident snapshot</h2>
          <p className={"connection " + connection.toLowerCase()}>
            {connection === "LIVE" ? "Live polling — server snapshot authoritative"
              : connection === "STALE" ? "Connection stale — action controls disabled"
              : "Synchronizing incident events"}
          </p>
          {error && <p className="error" role="alert">{error}</p>}
          {incident ? <>
            <span className="status">{incident.status_projection.replaceAll("_", " ")}</span>
            <dl>
              <dt>Incident ID</dt><dd className="mono">{incident.id}</dd>
              <dt>Target</dt><dd>{incident.target_id}</dd>
              <dt>Version</dt><dd>{incident.status_version}</dd>
              <dt>Created</dt><dd>{new Date(incident.created_at).toLocaleString()}</dd>
            </dl>
            <div className="actions">
              <button type="button" className="secondary" disabled={busy} onClick={refresh}>Refresh status</button>
              {incident.status_projection === "AWAITING_APPROVAL" && (
                <>
                  {!plan && <button type="button" disabled={busy || connection !== "LIVE"} onClick={() => decide("acknowledge")}>Acknowledge review</button>}
                </>
              )}
              {["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL"].includes(incident.status_projection) && (
                <button type="button" className="secondary" disabled={busy || connection !== "LIVE"} onClick={() => decide("cancel")}>Request cancellation</button>
              )}
            </div>
            {plan && <div className="plan">
              <h3>Proposed simulator plan</h3>
              <p><strong>{plan.action_id}</strong> for {plan.target_id}</p>
              <p className="muted">Plan state: {plan.status} · Expected target version: {plan.expected_version}</p>
              <p className="muted">Digest</p><code>{plan.plan_digest}</code>
              {plan.status === "PROPOSED" && incident.status_projection === "AWAITING_APPROVAL" &&
                <div className="actions">
                  <button type="button" disabled={busy || connection !== "LIVE"} onClick={() => decidePlan("APPROVE")}>Approve simulator plan</button>
                  <button type="button" className="secondary" disabled={busy || connection !== "LIVE"} onClick={() => decidePlan("REJECT")}>Reject</button>
                </div>
              }
            </div>}
            {evidence.length > 0 && <div className="audit">
              <h3>Evidence ({evidence.length})</h3>
              {evidence.map(item => <div className="audit-item" key={item.id}>
                <strong>{item.source}</strong><span>{item.validity}</span>
                <code>{item.redacted_excerpt}</code>
              </div>)}
            </div>}
            {lineage && <div className="audit">
              <h3>Evidence-to-effect trace</h3>
              <p className="muted">{lineage.projection.replaceAll("_", " ")} · {lineage.nodes.length} durable records · {lineage.edges.length} links</p>
              {!lineage.trace_spans_available && <p className="muted">Telemetry spans unavailable; durable records are shown instead.</p>}
              {lineage.missing_refs.length > 0 &&
                <p className="error">{lineage.missing_refs.length} missing durable link(s). Do not infer the omitted evidence.</p>}
              <div className="trace-scroll">
                {lineage.nodes.map(node => <div className="trace-node" key={node.id}>
                  <strong>{node.kind}</strong><span>{node.status}</span>
                  <code>{node.id.split(":")[1]}</code>
                </div>)}
              </div>
            </div>}
            {effects.length > 0 && <div className="audit">
              <h3>Effect receipts</h3>
              {effects.map(effect => <div className="audit-item" key={effect.id}>
                <strong>{effect.adapter_id}</strong><span>{effect.state}</span>
                <code>Observed version: {effect.observed_version ?? "unknown"}</code>
              </div>)}
            </div>}
          </> : <p className="muted empty">No incident selected yet.</p>}
        </section>
      </div>
      <section className="incident-history">
        <div className="history-header">
          <h2>Recent incidents</h2>
          <button type="button" className="secondary" disabled={!token || busy} onClick={loadRecent}>
            Load from server
          </button>
        </div>
        {recent.length === 0 ? <p className="muted">Load the latest tenant-scoped incidents.</p> :
          <div className="recent-items">
            {recent.map(item => (
              <button type="button" className="recent-item" key={item.id}
                onClick={() => chooseIncident(item)}>
                <strong>{item.target_id}</strong>
                <span>{item.status_projection.replaceAll("_", " ")}</span>
                <code>{item.id}</code>
              </button>
            ))}
          </div>
        }
        {incident && <div className="audit">
          <h3>Durable event history</h3>
          {events.length === 0 ? <p className="muted">No events loaded yet.</p> :
            events.map(event => <div className="audit-item" key={event.id}>
              <strong>#{event.id} — {event.kind.replaceAll("_", " ")}</strong>
              <span>Version {event.projection_version}</span>
            </div>)
          }
        </div>}
      </section>
      <footer>Simulator-only recovery. Real targets, production identities, AI model integration and independently calibrated evaluation are not enabled.</footer>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
