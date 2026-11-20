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

type ReplayFixture = "recoverable-v1" | "false-ack-v1" | "already-healthy-v1";
type ReplayReport = {
  id: string;
  fixture_id: ReplayFixture;
  status: string;
  seed: number;
  measured_ai_comparison: boolean;
  arms: {
    kind: string;
    outcome: string;
    probe_cost: number;
    oracle_passed: boolean;
    virtual_time: number;
  }[];
};

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
  const [replayFixture, setReplayFixture] = useState<ReplayFixture>("recoverable-v1");
  const [replaySeed, setReplaySeed] = useState("42");
  const [replay, setReplay] = useState<ReplayReport | null>(null);
  const [replayError, setReplayError] = useState("");
  const [replayBusy, setReplayBusy] = useState(false);
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

    const controller = new AbortController();
    const auth = { "X-Tenant-Id": tenant, "X-OpsBlaze-Token": token };
    const url = "/v1/incidents/" + selectedId;
    let cursor = 0;
    setConnection("SYNCING");

    async function refreshSnapshot() {
      const response = await fetch(url, { headers: auth, signal: controller.signal });
      const body: unknown = await response.json();
      if (!response.ok) throw new Error(detail(body));
      if (controller.signal.aborted) return;
      const incoming = body as Incident;
      setIncident(previous =>
        previous && previous.id === incoming.id
          && previous.status_version > incoming.status_version
          ? previous : incoming
      );
    }

    async function recordEvents(incoming: UIEvent[]) {
      const unseen = incoming.filter(event => event.id > cursor);
      if (unseen.length === 0 || controller.signal.aborted) return;
      cursor = unseen[unseen.length - 1].id;
      setEvents(previous => [...previous, ...unseen].slice(-20));
      // Event payloads never authorize controls; always fetch current truth.
      await refreshSnapshot();
    }

    async function pollFallback() {
      const response = await fetch(url + "/events?after_id=" + cursor, {
        headers: auth, signal: controller.signal
      });
      const body: unknown = await response.json();
      if (!response.ok) throw new Error(detail(body));
      const page = body as UIEventPage;
      await recordEvents(page.events);
      cursor = Math.max(cursor, page.last_id);
      if (!controller.signal.aborted) setConnection("LIVE");
    }

    async function subscribe() {
      while (!controller.signal.aborted) {
        try {
          const response = await fetch(url + "/stream?after_id=" + cursor, {
            headers: { ...auth, "Accept": "text/event-stream" },
            signal: controller.signal
          });
          if (!response.ok || !response.body) throw new Error("stream unavailable");
          setConnection("LIVE");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let pending = "";
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            pending += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
            let boundary = pending.indexOf("\n\n");
            while (boundary !== -1) {
              const frame = pending.slice(0, boundary);
              pending = pending.slice(boundary + 2);
              const data = frame.split("\n").find(line => line.startsWith("data: "));
              if (data) {
                const event = JSON.parse(data.slice(6)) as UIEvent;
                if (Number.isSafeInteger(event.id) && event.id > cursor) {
                  await recordEvents([event]);
                }
              }
              boundary = pending.indexOf("\n\n");
            }
            // A malformed or oversized frame must not consume unbounded memory.
            if (pending.length > 16384) throw new Error("invalid event stream");
          }
        } catch {
          if (controller.signal.aborted) break;
          setConnection("STALE");
          try {
            await pollFallback();
          } catch {
            if (controller.signal.aborted) break;
            setConnection("STALE");
          }
        }
        if (!controller.signal.aborted) {
          await new Promise(resolve => window.setTimeout(resolve, 2000));
        }
      }
    }

    void subscribe();
    return () => controller.abort();
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


  async function runReplay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const seed = Number(replaySeed);
    if (!Number.isInteger(seed) || seed < 0 || seed > 1000000) {
      setReplayError("Seed must be an integer from 0 to 1,000,000.");
      return;
    }
    setReplayBusy(true);
    setReplayError("");
    setReplay(null);
    try {
      const created = await fetch("/v1/experiments", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ fixture_id: replayFixture, seed })
      });
      const started: unknown = await created.json();
      if (!created.ok) throw new Error(detail(started));
      const id = (started as { id: string }).id;
      const response = await fetch("/v1/experiments/" + id + "/results", {
        headers: headers()
      });
      const result: unknown = await response.json();
      if (!response.ok) throw new Error(detail(result));
      setReplay(result as ReplayReport);
    } catch (cause) {
      setReplayError(cause instanceof Error ? cause.message : "Replay failed");
    } finally {
      setReplayBusy(false);
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
            {connection === "LIVE" ? "Live updates — server snapshot authoritative"
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
      <section className="incident-history replay-lab">
        <h2>Isolated replay laboratory</h2>
        <p className="muted">
          Run two deterministic simulator arms with the same initial fixture,
          virtual-time evidence release, and independent postcondition.
          This is not a measured AI-versus-human experiment.
        </p>
        <form className="replay-form" onSubmit={runReplay}>
          <label>Fixture
            <select value={replayFixture} onChange={event => setReplayFixture(event.target.value as ReplayFixture)}>
              <option value="recoverable-v1">Recoverable fault</option>
              <option value="false-ack-v1">False success acknowledgment</option>
              <option value="already-healthy-v1">Healthy target</option>
            </select>
          </label>
          <label>Seed
            <input type="number" min="0" max="1000000" step="1"
              value={replaySeed} onChange={event => setReplaySeed(event.target.value)} />
          </label>
          <button type="submit" disabled={!token || replayBusy}>
            {replayBusy ? "Running..." : "Run paired replay"}
          </button>
        </form>
        {replayError && <p role="alert" className="error">{replayError}</p>}
        {replay && <>
          <p className="muted">Experiment {replay.id} · {replay.status} · Seed {replay.seed}</p>
          <div className="replay-results">
            {replay.arms.map(arm => (
              <div className="replay-arm" key={arm.kind}>
                <strong>{arm.kind.replaceAll("_", " ")}</strong>
                <span>{arm.outcome}</span>
                <p className="muted">Oracle passed: {arm.oracle_passed ? "Yes" : "No"} · Read probes: {arm.probe_cost} · Virtual time: {arm.virtual_time}</p>
              </div>
            ))}
          </div>
          {!replay.measured_ai_comparison &&
            <p className="muted">Deterministic baseline only; no production or model performance claim.</p>
          }
        </>}
      </section>
      <footer>Simulator-only recovery. Real targets, production identities, calibrated model evaluation and policy rollout are not enabled.</footer>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
