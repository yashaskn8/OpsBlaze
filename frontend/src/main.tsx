import { useEffect, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import {
  currentAccessToken, endLocalSession, finishLoginCallback,
  oidcManager, productionIdentityMode, startLogin,
} from "./auth";
import { authManager, oidcConfigError, usingOidc } from "./auth";

type AuthIdentity = {
  tenant_id: string;
  actor_id: string;
  roles: string[];
};

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

type Approval = {
  id: string;
  state: "APPROVED" | "REJECTED" | "REVOKED" | "CONSUMED";
  version: number;
  expires_at: string;
  reviewer: string;
  scope: string;
  reason: string;
};

type PlanDetail = Plan & { approval: Approval | null };

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
  const [reviewToken, setReviewToken] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const authenticated = productionIdentityMode ? Boolean(accessToken) : Boolean(token);
  const canReview = productionIdentityMode ? authenticated : Boolean(reviewToken);
  const [accessToken, setAccessToken] = useState("");
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [signInError, setSignInError] = useState("");
  const [authLoading, setAuthLoading] = useState(usingOidc);

  const [target, setTarget] = useState("checkout");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [incident, setIncident] = useState<Incident | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [decisionReason, setDecisionReason] = useState(
    "Reviewed simulator evidence and expected target version"
  );
  const [revocationReason, setRevocationReason] = useState(
    "Reviewer withdrew permission before simulator dispatch"
  );
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

  const headers = (): Record<string, string> => usingOidc
    ? { Authorization: "Bearer " + accessToken }
    : { "X-Tenant-Id": tenant, "X-OpsBlaze-Token": token };
  const authenticated = usingOidc
    ? Boolean(accessToken && identity)
    : Boolean(token && tenant);
  const canOperate = usingOidc ? Boolean(identity?.roles.includes("operator")) : authenticated;
  const canReview = usingOidc ? Boolean(identity?.roles.includes("reviewer")) : Boolean(reviewToken);

  useEffect(() => {
    if (!usingOidc) return;
    if (!authManager) {
      setAuthLoading(false);
      setSignInError(oidcConfigError);
      return;
    }
    let active = true;
    const manager = authManager;
    async function loadOidcSession() {
      try {
        const user = window.location.pathname === "/auth/callback"
          ? await manager.signinRedirectCallback()
          : await manager.getUser();
        if (!active) return;
        if (user && !user.expired && user.access_token) {
          setAccessToken(user.access_token);
        } else {
          setAccessToken("");
          setIdentity(null);
        }
      } catch (cause) {
        if (active) setSignInError(
          cause instanceof Error ? cause.message : "Sign-in failed"
        );
      } finally {
        if (window.location.pathname === "/auth/callback") {
          // Remove the authorization code and state from browser history.
          window.history.replaceState(null, "", "/");
        }
        if (active) setAuthLoading(false);
      }
    }
    void loadOidcSession();
    const expired = () => { setAccessToken(""); setIdentity(null); };
    manager.events.addAccessTokenExpired(expired);
    return () => {
      active = false;
      manager.events.removeAccessTokenExpired(expired);
    };
  }, []);

  useEffect(() => {
    if (!usingOidc || !accessToken) {
      setIdentity(null);
      return;
    }
    const controller = new AbortController();
    async function verifyIdentity() {
      try {
        const response = await fetch("/v1/auth/me", {
          headers: { Authorization: "Bearer " + accessToken },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Access token rejected by OpsBlaze API");
        const actor = await response.json() as AuthIdentity;
        if (!controller.signal.aborted) {
          setTenant(actor.tenant_id);
          setIdentity(actor);
          setSignInError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          setIdentity(null);
          setAccessToken("");
          setSignInError(cause instanceof Error ? cause.message : "Identity check failed");
        }
      }
    }
    void verifyIdentity();
    return () => controller.abort();
  }, [accessToken]);

  async function signIn() {
    if (!authManager) return;
    setSignInError("");
    try {
      await authManager.signinRedirect();
    } catch (cause) {
      setSignInError(cause instanceof Error ? cause.message : "Sign-in failed");
    }
  }

  async function signOut() {
    await authManager?.removeUser();
    setAccessToken("");
    setIdentity(null);
    setIncident(null);
    setPlan(null);
    setApproval(null);
    setEvents([]);
    setConnection("SYNCING");
  }

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
      setApproval(null);
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
    if (!incident || !canOperate) return;
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
      if (plans[0]) {
        const planResponse = await fetch("/v1/plans/" + plans[0].id, {
          headers: headers()
        });
        const detailBody: unknown = await planResponse.json();
        if (!planResponse.ok) throw new Error(detail(detailBody));
        setApproval((detailBody as PlanDetail).approval);
      } else {
        setApproval(null);
      }
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
    setApproval(null);
    setEvidence([]);
    setEffects([]);
    setLineage(null);
    setEvents([]);
    setConnection("SYNCING");
  }


  useEffect(() => {
    const selectedId = incident?.id;
    if (!selectedId || !authenticated) return;

    const controller = new AbortController();
    const auth = headers();
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

    async function resyncAfterGap(response: Response): Promise<boolean> {
      if (response.status !== 410) return false;
      const payload: unknown = await response.json();
      const minimum = (payload as { detail?: { min_after_id?: number } })
        ?.detail?.min_after_id;
      if (!Number.isSafeInteger(minimum) || minimum === undefined || minimum < 0) {
        throw new Error("invalid event retention watermark");
      }
      cursor = Math.max(cursor, minimum);
      setEvents([]);
      setConnection("SYNCING");
      await refreshSnapshot();
      return true;
    }

    async function pollFallback() {
      const response = await fetch(url + "/events?after_id=" + cursor, {
        headers: auth, signal: controller.signal
      });
      if (await resyncAfterGap(response)) return;
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
          if (await resyncAfterGap(response)) continue;
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
              const lines = frame.split("\n");
              const data = lines.find(line => line.startsWith("data: "));
              if (data && lines.includes("event: gap")) {
                const gap = JSON.parse(data.slice(6)) as { min_after_id: number };
                if (!Number.isSafeInteger(gap.min_after_id) || gap.min_after_id < 0) {
                  throw new Error("invalid gap notification");
                }
                cursor = Math.max(cursor, gap.min_after_id);
                setEvents([]);
                setConnection("SYNCING");
                await refreshSnapshot();
              } else if (data) {
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
  }, [incident?.id, tenant, token, accessToken, identity?.actor_id]);

  async function decide(action: "cancel" | "acknowledge") {
    if (!incident || !authenticated) return;
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
    if (!plan || !incident || !canReview) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/plans/" + plan.id + "/decisions", {
        method: "POST",
        headers: {
          ...headers(),
          "Content-Type": "application/json",
          "If-Match": String(plan.version),
          ...(usingOidc ? {} : { "X-OpsBlaze-Reviewer-Token": reviewToken }),
        },
        body: JSON.stringify({
          decision: choice, reason: decisionReason.trim()
        })
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



  async function revokeApproval() {
    if (!approval || !plan) return;
    if (revocationReason.trim().length < 8) {
      setError("Revocation reason must be at least 8 characters.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        "/v1/approvals/" + approval.id + "/revoke", {
          method: "POST",
          headers: {
            ...headers(),
            ...(usingOidc ? {} : { "X-OpsBlaze-Reviewer-Token": reviewToken }),
            "If-Match": String(approval.version),
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ reason: revocationReason.trim() })
        }
      );
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Approval revocation failed");
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
          <h2>New incident</h2><p className="muted">{usingOidc
            ? "Verified role-based access. Access tokens stay in browser memory."
            : "Development only. Token stays in browser memory."}</p>
          {usingOidc && <div className="auth-session" aria-live="polite">
            {authLoading && <p>Checking sign-in…</p>}
            {signInError && <p role="alert" className="error">{signInError}</p>}
            {identity ? <>
              <p>Signed in · Tenant <strong>{identity.tenant_id}</strong></p>
              <p className="muted">Roles: {identity.roles.join(", ")}</p>
              <p className="muted mono">{identity.actor_id}</p>
              <button className="secondary" type="button" onClick={() => void signOut()}>
                Clear browser session
              </button>
            </> : !authLoading && authManager && <button type="button"
              onClick={() => void signIn()}>Sign in with identity provider</button>}
          </div>}
          <form onSubmit={admit}>
            {!usingOidc && <>
              <label>Tenant ID<input required maxLength={64} value={tenant} onChange={e => setTenant(e.target.value)} /></label>
              <label>Local API token<input type="password" autoComplete="off" required value={token} onChange={e => setToken(e.target.value)} /></label>
              <label>Local reviewer token<input type="password" autoComplete="off"
                value={reviewToken} onChange={e => setReviewToken(e.target.value)} /></label>
            </>}
            <label>Target ID<input required maxLength={256} value={target} onChange={e => setTarget(e.target.value)} /></label>
            <p className="key">Idempotency key <code>{key}</code></p>
            <div className="actions">
              <button type="submit" disabled={busy || !canOperate}>{busy ? "Working..." : "Admit incident"}</button>
              <button type="button" className="secondary" disabled={busy} onClick={() => { setKey(crypto.randomUUID()); setIncident(null); setPlan(null); setApproval(null); setEvidence([]); setEffects([]); setLineage(null); setEvents([]); setConnection("SYNCING"); setError(""); }}>New request key</button>
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
                  {!plan && <button type="button" disabled={busy || !canOperate || connection !== "LIVE"} onClick={() => decide("acknowledge")}>Acknowledge review</button>}
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
                <label className="review-reason">Review reason
                  <textarea minLength={8} maxLength={256}
                    value={decisionReason}
                    onChange={event => setDecisionReason(event.target.value)}
                    placeholder="What evidence did you review?" />
                </label>}
              {plan.status === "PROPOSED" && incident.status_projection === "AWAITING_APPROVAL" &&
                <div className="actions">
                  <button type="button" disabled={busy || connection !== "LIVE" || !canReview || decisionReason.trim().length < 8} onClick={() => decidePlan("APPROVE")}>Approve simulator plan</button>
                  <button type="button" className="secondary" disabled={busy || connection !== "LIVE" || !canReview || decisionReason.trim().length < 8} onClick={() => decidePlan("REJECT")}>Reject</button>
                </div>
              }
              {approval && <div className="approval-review">
                <h4>Durable reviewer decision</h4>
                <p className="muted">Status: {approval.state} · Version {approval.version}</p>
                <p className="muted">Reviewer ID: {approval.reviewer}</p>
                <p className="muted">Scope: {approval.scope}</p>
                <p className="muted">Expires: {new Date(approval.expires_at).toLocaleString()}</p>
                <p className="muted">Reason: {approval.reason}</p>
                {approval.state === "APPROVED" && <>
                  <label>Revocation reason
                    <textarea minLength={8} maxLength={256}
                      value={revocationReason}
                      onChange={event => setRevocationReason(event.target.value)}
                    />
                  </label>
                  <button type="button" className="secondary"
                    disabled={busy || connection !== "LIVE" || !canReview || revocationReason.trim().length < 8}
                    onClick={revokeApproval}>
                    Revoke unconsumed approval
                  </button>
                </>}
              </div>}
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
          <button type="button" className="secondary" disabled={!authenticated || busy} onClick={loadRecent}>
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
          <button type="submit" disabled={!canOperate || replayBusy}>
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
      <footer>Simulator-only recovery. External identity integration must be configured and verified for production use. Real targets, calibrated evaluation and policy rollout remain disabled.</footer>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
