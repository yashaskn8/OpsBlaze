import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { evidenceProjectionWarning, replaceLineagePage, type EvidenceProjectionStatus } from "./lineagePages";
import { authManager, oidcConfigError, usingOidc } from "./auth";
import { consumeIncidentStream } from "./incidentStream";
import { InvestigationPanel } from "./InvestigationPanel";
import {
  detail, fetchQueueHealth, fetchDeadLetters, fetchInvestigation, fetchLineageRecordDetail, finishReview,
  type Incident, type Investigation, type LineageDiagnostics, type LineageRecordDetail,
  type QueueHealth, type ExperienceDeadLetter, type ExperienceDeadLetterPage,
} from "./api";

type AuthIdentity = {
  tenant_id: string;
  actor_id: string;
  roles: string[];
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
  decision: "APPROVE" | "REJECT";
  decided_at: string;
  reviewer: string;
  scope: string;
  reason: string;
};

type PlanDetail = Plan & {
  incident_id: string;
  approval: Approval | null;
  review: {
    state: "DRAFT" | "PENDING" | "EXPIRED" | "INVALIDATED" | Approval["state"];
    pending_since: string | null;
    evaluated_at: string;
  };
  policy_id: string | null;
  postcondition: { healthy: boolean; version: number } | null;
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

type TraceRef = { node_id: string; trace_id: string; span_id: string; collector_verified: false };

type Lineage = {
  projection: "COMPLETE" | "PARTIAL_LINKS";
  evidence_projection?: EvidenceProjectionStatus;
  diagnostics?: LineageDiagnostics;
  trace_spans_available: boolean;
  trace_refs: TraceRef[];
  links_truncated: boolean;
  truncated_sources: string[];
  nodes: { id: string; kind: string; status: string }[];
  edges: { from: string; to: string; relation: string }[];
  missing_refs: { source: string; missing_target: string; relation: string }[];
};

type LineagePage = {
  evidence_projection?: EvidenceProjectionStatus;
  diagnostics?: LineageDiagnostics;
  incident_id: string;
  kind: string;
  nodes: Lineage["nodes"];
  trace_refs: TraceRef[];
  edges: Lineage["edges"];
  missing_refs: Lineage["missing_refs"];
  links_truncated: boolean;
  next_cursor: string | null;
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

function App() {
  const [tenant, setTenant] = useState("demo");
  const [token, setToken] = useState("");
  const [reviewToken, setReviewToken] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [signInError, setSignInError] = useState("");
  const [authLoading, setAuthLoading] = useState(usingOidc);

  const [target, setTarget] = useState("checkout");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [incident, setIncident] = useState<Incident | null>(null);
  const [reviewSnapshot, setReviewSnapshot] = useState<{
    scope: object; incidentId: string; plan: PlanDetail | null; expiresAt: number | null;
  } | null>(null);
  const [expiredReview, setExpiredReview] = useState<typeof reviewSnapshot>(null);
  const [reviewStale, setReviewStale] = useState(false);
  const [decisionReason, setDecisionReason] = useState(
    "Reviewed simulator evidence and expected target version"
  );
  const [revocationReason, setRevocationReason] = useState(
    "Reviewer withdrew permission before simulator dispatch"
  );
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [effects, setEffects] = useState<Effect[]>([]);
  const [lineage, setLineage] = useState<Lineage | null>(null);
  const [lineagePages, setLineagePages] = useState<{
    incidentId: string; pages: Record<string, LineagePage>;
  }>({ incidentId: "", pages: {} });
  const [selectedRecord, setSelectedRecord] = useState<{
    scope: object; incidentId: string; record: LineageRecordDetail;
  } | null>(null);
  const [recordBusy, setRecordBusy] = useState(false);
  const [recordError, setRecordError] = useState("");
  const recordRequestVersion = useRef(0);
  const [recent, setRecent] = useState<Incident[]>([]);
  const [queueHealth, setQueueHealth] = useState<QueueHealth | null>(null);
  const [queueBusy, setQueueBusy] = useState(false);
  const [queueError, setQueueError] = useState("");
  const queueRequestVersion = useRef(0);
  const [deadLetterPage, setDeadLetterPage] = useState<ExperienceDeadLetterPage | null>(null);
  const [deadLetterBusy, setDeadLetterBusy] = useState(false);
  const [deadLetterError, setDeadLetterError] = useState("");
  const [redriveReason, setRedriveReason] = useState<
    "STORE_RESTORED" | "TRANSIENT_FIX_VERIFIED"
  >("STORE_RESTORED");
  const deadLetterRequestVersion = useRef(0);
  const [events, setEvents] = useState<UIEvent[]>([]);
  const [connection, setConnection] = useState<"SYNCING" | "LIVE" | "STALE">("SYNCING");
  const authScope = useMemo(() => ({}), [tenant, token, accessToken, identity?.actor_id]);
  const selectedReview = reviewSnapshot?.scope === authScope && reviewSnapshot.incidentId === incident?.id
    ? reviewSnapshot : null;
  const plan = selectedReview?.plan ?? null;
  const approval = plan?.approval ?? null;
  const reviewState = selectedReview && expiredReview === selectedReview ? "EXPIRED" : plan?.review.state;
  const [investigation, setInvestigation] = useState<{ scope: object; report: Investigation } | null>(null);
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
  const activeInvestigation = authenticated && investigation?.scope === authScope
    && investigation.report.incident.id === incident?.id ? investigation.report : null;
  const canOperate = usingOidc ? Boolean(identity?.roles.includes("operator")) : authenticated;
  const canRedrive = usingOidc && Boolean(
    identity?.roles.includes("security_admin")
    && !identity.roles.includes("operator")
    && !identity.roles.includes("reviewer")
  );
  const canReview = usingOidc
    ? Boolean(
        identity?.roles.includes("reviewer")
        && !identity.roles.includes("operator")
        && !identity.roles.includes("security_admin")
      )
    : Boolean(reviewToken);

  useEffect(() => {
    const expiresAt = selectedReview?.expiresAt;
    if (expiresAt == null) return;
    const timer = window.setTimeout(() => setExpiredReview(selectedReview), expiresAt - performance.now());
    return () => window.clearTimeout(timer);
  }, [selectedReview]);

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
    setReviewSnapshot(null);
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
      setReviewSnapshot(null);
      setReviewStale(false);
      setEvidence([]);
      setEffects([]);
      setLineage(null);
      setLineagePages({ incidentId: "", pages: {} });
      setEvents([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    // Viewer and reviewer roles may inspect authoritative evidence.
    // Only the API grants mutation rights; this is read-only.
    if (!incident || !authenticated) return;
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
      let preview: PlanDetail | null = null;
      let expiresAt: number | null = null;
      if (plans[0]) {
        const planResponse = await fetch("/v1/plans/" + plans[0].id, {
          headers: headers()
        });
        const receivedAt = performance.now();
        const detailBody: unknown = await planResponse.json();
        if (!planResponse.ok) throw new Error(detail(detailBody));
        preview = detailBody as PlanDetail;
        if (preview.id !== plans[0].id || preview.incident_id !== incident.id) {
          throw new Error("Plan preview did not match the selected incident");
        }
        if (preview.review.state === "APPROVED" && preview.approval) {
          const remaining = Date.parse(preview.approval.expires_at) - Date.parse(preview.review.evaluated_at);
          expiresAt = receivedAt + remaining;
        }
      }
      setReviewSnapshot({ scope: authScope, incidentId: incident.id, plan: preview, expiresAt });
      setReviewStale(false);
      setEvidence(proof);
      setEffects(receipts);
      const lineageResponse = await fetch(
        "/v1/incidents/" + incident.id + "/lineage", { headers: headers() }
      );
      const lineageBody: unknown = await lineageResponse.json();
      if (!lineageResponse.ok) throw new Error(detail(lineageBody));
      setLineage(lineageBody as Lineage);
      setLineagePages({ incidentId: "", pages: {} });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }



  async function loadMoreLineage(kind: string) {
    if (!incident || !authenticated || !lineage || busy) return;
    const previous = lineagePages.incidentId === incident.id
      ? lineagePages.pages[kind] : undefined;
    if (previous && previous.next_cursor === null) return;
    const firstPageLast = lineage.nodes.filter(node => node.kind === kind).at(-1);
    const cursor = previous?.next_cursor ?? firstPageLast?.id.split(":")[1];
    if (!cursor) return;
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({ kind, cursor, limit: "50" });
      const response = await fetch(
        "/v1/incidents/" + incident.id + "/lineage/records?" + query.toString(),
        { headers: headers() }
      );
      const result: unknown = await response.json();
      if (!response.ok) throw new Error(detail(result));
      const page = result as LineagePage;
      if (page.incident_id !== incident.id || page.kind !== kind) {
        throw new Error("Lineage page did not match the selected incident");
      }
      setLineagePages(current => replaceLineagePage(current, page));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load lineage page");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    // Never display a queue snapshot under different browser credentials.
    queueRequestVersion.current += 1;
    deadLetterRequestVersion.current += 1;
    recordRequestVersion.current += 1;
    setQueueHealth(null);
    setQueueError("");
    setQueueBusy(false);
    setDeadLetterPage(null);
    setDeadLetterError("");
    setDeadLetterBusy(false);
    setSelectedRecord(null);
    setRecordError("");
    setRecordBusy(false);
  }, [tenant, token, accessToken]);

  async function inspectLineageNode(kind: string, recordId: string) {
    if (!incident || !authenticated || kind === "incident") return;
    const requestVersion = ++recordRequestVersion.current;
    const incidentId = incident.id;
    const scope = authScope;
    setRecordBusy(true);
    setRecordError("");
    try {
      const record = await fetchLineageRecordDetail(incidentId, kind, recordId, headers());
      if (recordRequestVersion.current === requestVersion) {
        setSelectedRecord({ scope, incidentId, record });
      }
    } catch (cause) {
      if (recordRequestVersion.current === requestVersion) {
        setSelectedRecord(null);
        setRecordError(cause instanceof Error ? cause.message : "Could not load record detail");
      }
    } finally {
      if (recordRequestVersion.current === requestVersion) setRecordBusy(false);
    }
  }

  async function loadQueueHealth() {
    if (!authenticated) return;
    const requestVersion = ++queueRequestVersion.current;
    setQueueBusy(true);
    setQueueError("");
    setQueueHealth(null);
    try {
      const data = await fetchQueueHealth(headers());
      if (queueRequestVersion.current === requestVersion) {
        setQueueHealth(data);
      }
    } catch (cause) {
      if (queueRequestVersion.current === requestVersion) {
        setQueueError(
          cause instanceof Error ? cause.message : "Queue health unavailable"
        );
      }
    } finally {
      if (queueRequestVersion.current === requestVersion) setQueueBusy(false);
    }
  }

  async function loadDeadLetters(more = false) {
    if (!authenticated || deadLetterBusy) return;
    const cursor = more ? deadLetterPage?.next_cursor : 0;
    if (cursor === null || cursor === undefined) return;
    const requestVersion = ++deadLetterRequestVersion.current;
    setDeadLetterBusy(true);
    setDeadLetterError("");
    try {
      const page = await fetchDeadLetters(headers(), cursor);
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterPage(previous => ({
          ...page,
          records: more
            ? [...(previous?.records ?? []), ...page.records]
            : page.records,
        }));
      }
    } catch (cause) {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterError(
          cause instanceof Error ? cause.message : "Dead-letter history unavailable"
        );
      }
    } finally {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterBusy(false);
      }
    }
  }

  async function acknowledgeDeadLetter(record: ExperienceDeadLetter) {
    if (!canOperate || deadLetterBusy || record.reviewed) return;
    const requestVersion = ++deadLetterRequestVersion.current;
    setDeadLetterBusy(true);
    setDeadLetterError("");
    try {
      const response = await fetch(
        "/v1/operations/dead-letters/" + record.id + "/acknowledge",
        { method: "POST", headers: {
          ...headers(), "If-Match": String(record.attempts),
        }},
      );
      const result: unknown = await response.json();
      if (!response.ok) throw new Error(detail(result));
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterPage(previous => previous && ({
          ...previous,
          records: previous.records.map(item =>
            item.id === record.id ? { ...item, reviewed: true } : item
          ),
        }));
        setQueueHealth(previous => previous && ({
          ...previous,
          experience_dead_letters_unreviewed: Math.max(
            0, previous.experience_dead_letters_unreviewed - 1
          ),
        }));
      }
    } catch (cause) {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterError(
          cause instanceof Error ? cause.message : "Review acknowledgment failed"
        );
      }
    } finally {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterBusy(false);
      }
    }
  }

  async function redriveDeadLetter(record: ExperienceDeadLetter) {
    if (
      !canRedrive || deadLetterBusy || !record.reviewed
      || record.redriven_to !== null || record.code !== "RETRY_EXHAUSTED"
    ) return;
    const requestVersion = ++deadLetterRequestVersion.current;
    setDeadLetterBusy(true);
    setDeadLetterError("");
    try {
      const response = await fetch(
        "/v1/operations/dead-letters/" + record.id + "/redrive",
        { method: "POST", headers: {
          ...headers(), "Content-Type": "application/json",
          "If-Match": String(record.attempts),
        }, body: JSON.stringify({ reason: redriveReason }) },
      );
      const data: unknown = await response.json();
      if (!response.ok) throw new Error(detail(data));
      const submitted = data as {
        id: number; source_id: number; state: string;
      };
      if (submitted.source_id !== record.id || submitted.state !== "PENDING_PROJECTION") {
        throw new Error("Unexpected projection redrive response");
      }
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterPage(previous => previous && ({
          ...previous,
          records: previous.records.map(item =>
            item.id === record.id
              ? { ...item, redriven_to: submitted.id }
              : item,
          ),
        }));
      }
    } catch (cause) {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterError(
          cause instanceof Error ? cause.message : "Redrive request rejected",
        );
      }
    } finally {
      if (deadLetterRequestVersion.current === requestVersion) {
        setDeadLetterBusy(false);
      }
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
    setReviewSnapshot(null);
    setReviewStale(false);
    setEvidence([]);
    setEffects([]);
    setLineage(null);
    setLineagePages({ incidentId: "", pages: {} });
    setEvents([]);
    setConnection("SYNCING");
  }


  useEffect(() => {
    const selectedId = incident?.id;
    if (!selectedId || !authenticated) return;
    const incidentId = selectedId;

    const controller = new AbortController();
    const auth = headers();
    const url = "/v1/incidents/" + selectedId;
    let cursor = 0;
    setConnection("SYNCING");

    async function refreshSnapshot() {
      const snapshot = await fetchInvestigation(incidentId, auth, controller.signal);
      if (controller.signal.aborted) return;
      const incoming = snapshot.incident;
      setIncident(previous =>
        previous?.id === incoming.id && previous.status_version <= incoming.status_version
          ? incoming : previous
      );
      setInvestigation(previous =>
        previous?.scope === authScope && previous.report.incident.id === incoming.id
          && previous.report.incident.status_version > incoming.status_version
          ? previous : { scope: authScope, report: snapshot }
      );
    }

    async function recordEvents(incoming: UIEvent[]) {
      const unseen = incoming.filter(event => event.id > cursor);
      if (unseen.length === 0 || controller.signal.aborted) return;
      // An event cannot advance the cursor until its authoritative snapshot
      // has been fetched; failed fetches must be retried on reconnect.
      await refreshSnapshot();
      if (controller.signal.aborted) return;
      cursor = unseen[unseen.length - 1].id;
      setEvents(previous => [...previous, ...unseen].slice(-20));
    }

    async function resyncAfterGap(response: Response): Promise<boolean> {
      if (response.status !== 410) return false;
      const payload: unknown = await response.json();
      const minimum = (payload as { detail?: { min_after_id?: number } })
        ?.detail?.min_after_id;
      if (!Number.isSafeInteger(minimum) || minimum === undefined || minimum < 0) {
        throw new Error("invalid event retention watermark");
      }
      setConnection("SYNCING");
      await refreshSnapshot();
      if (controller.signal.aborted) return true;
      cursor = Math.max(cursor, minimum);
      setEvents([]);
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
      const hasNewEvents = page.events.some(event => event.id > cursor);
      await recordEvents(page.events);
      // An empty event page does not prove the cached incident is current.
      if (!hasNewEvents) await refreshSnapshot();
      if (controller.signal.aborted) return;
      cursor = Math.max(cursor, page.last_id);
      setConnection("LIVE");
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
          await consumeIncidentStream(response.body, controller.signal, {
            async onOpen() {
              setConnection("SYNCING");
              await refreshSnapshot();
              if (!controller.signal.aborted) setConnection("LIVE");
            },
            async onFrame(lines) {
              const data = lines.find(line => line.startsWith("data: "));
              if (data && lines.includes("event: gap")) {
                const gap = JSON.parse(data.slice(6)) as { min_after_id: number };
                if (!Number.isSafeInteger(gap.min_after_id) || gap.min_after_id < 0) {
                  throw new Error("invalid gap notification");
                }
                setConnection("SYNCING");
                await refreshSnapshot();
                if (controller.signal.aborted) return;
                cursor = Math.max(cursor, gap.min_after_id);
                setEvents([]);
              } else if (data) {
                const event = JSON.parse(data.slice(6)) as UIEvent;
                if (Number.isSafeInteger(event.id) && event.id > cursor) {
                  await recordEvents([event]);
                }
              }
            },
            onClose() { setConnection("STALE"); },
          });
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
          // Jitter prevents multiple operator tabs reconnecting in lockstep.
          await new Promise(resolve => window.setTimeout(
            resolve, 1500 + Math.floor(Math.random() * 1500),
          ));
        }
      }
    }

    void subscribe();
    return () => controller.abort();
  }, [incident?.id, authScope, authenticated]);

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
    if (!plan || !incident || !canReview || reviewStale || connection !== "LIVE") return;
    if (choice === "APPROVE" && (!plan.policy_id || !plan.postcondition)) return;
    setBusy(true);
    setReviewStale(true);
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
      await finishReview(response, refresh);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }



  async function revokeApproval() {
    if (!approval || reviewState !== "APPROVED" || !canReview || reviewStale || connection !== "LIVE") return;
    if (revocationReason.trim().length < 8) {
      setError("Revocation reason must be at least 8 characters.");
      return;
    }
    setBusy(true);
    setReviewStale(true);
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
      await finishReview(response, refresh);
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

  const additionalLineagePages = incident && lineagePages.incidentId === incident.id
    ? Object.values(lineagePages.pages) : [];
  const allLineageEdges = lineage
    ? [...lineage.edges, ...additionalLineagePages.flatMap(page => page.edges)]
    : [];

  return (
    <main>
      <a className="skip-link" href="#incident-snapshot">Skip to incident snapshot</a>
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
              <button type="button" className="secondary" disabled={busy} onClick={() => { setKey(crypto.randomUUID()); setIncident(null); setReviewSnapshot(null); setReviewStale(false); setEvidence([]); setEffects([]); setLineage(null); setLineagePages({ incidentId: "", pages: {} }); setEvents([]); setConnection("SYNCING"); setError(""); }}>New request key</button>
            </div>
          </form>
        </section>
        <section id="incident-snapshot" tabIndex={-1} aria-labelledby="incident-snapshot-title">
          <h2 id="incident-snapshot-title">Incident snapshot</h2>
          <p role="status" aria-live="polite" aria-atomic="true"
            className={"connection " + connection.toLowerCase()}>
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
                  {selectedReview && !plan && <button type="button" disabled={busy || reviewStale || !canOperate || connection !== "LIVE"} onClick={() => decide("acknowledge")}>Acknowledge review</button>}
                </>
              )}
              {["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL"].includes(incident.status_projection) && (
                <button type="button" className="secondary" disabled={busy || !canOperate || connection !== "LIVE"} onClick={() => decide("cancel")}>Request cancellation</button>
              )}
            </div>
            {activeInvestigation && <InvestigationPanel report={activeInvestigation}
              stale={connection !== "LIVE" || activeInvestigation.incident.status_version !== incident.status_version} />}
            {plan && <div className="plan">
              <h3>Proposed simulator plan</h3>
              <p><strong>{plan.action_id}</strong> for {plan.target_id}</p>
              <p className="muted">Plan state: {plan.status} · Expected target version: {plan.expected_version}</p>
              <p aria-live="polite">Review status: {reviewState}</p>
              {plan.review.pending_since && <p className="muted">
                Pending since {new Date(plan.review.pending_since).toLocaleString()}
              </p>}
              <p>Pinned policy: {plan.policy_id ?? "Unavailable"}</p>
              <p>Expected postcondition: {plan.postcondition
                ? `Simulator target healthy at version ${plan.postcondition.version}` : "Unavailable"}</p>
              {reviewStale && <p role="status" className="muted">Refresh the plan before reviewing it again.</p>}
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
                  <button type="button" disabled={busy || reviewStale || connection !== "LIVE" || !canReview || !plan.policy_id || !plan.postcondition || decisionReason.trim().length < 8} onClick={() => decidePlan("APPROVE")}>Approve simulator plan</button>
                  <button type="button" className="secondary" disabled={busy || reviewStale || connection !== "LIVE" || !canReview || decisionReason.trim().length < 8} onClick={() => decidePlan("REJECT")}>Reject</button>
                </div>
              }
              {approval && <div className="approval-review">
                <h4>Durable reviewer decision</h4>
                <p className="muted">Recorded decision: {approval.decision} · Version {approval.version}</p>
                <p className="muted">Decided: {new Date(approval.decided_at).toLocaleString()}</p>
                <p className="muted">Reviewer ID: {approval.reviewer}</p>
                <p className="muted">Scope: {approval.scope}</p>
                <p className="muted">Expires: {new Date(approval.expires_at).toLocaleString()}</p>
                <p className="muted">Reason: {approval.reason}</p>
                {reviewState === "APPROVED" && <>
                  <label>Revocation reason
                    <textarea minLength={8} maxLength={256}
                      value={revocationReason}
                      onChange={event => setRevocationReason(event.target.value)}
                    />
                  </label>
                  <button type="button" className="secondary"
                    disabled={busy || reviewStale || connection !== "LIVE" || !canReview || revocationReason.trim().length < 8}
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
              {lineage.diagnostics && <p className="muted">
                Diagnostics: {lineage.diagnostics.missing_edge_count} missing link(s) · {lineage.diagnostics.context_loss_count} missing span context(s) · projection lag {lineage.diagnostics.projection_lag_seconds ?? "unknown"}s
              </p>}
              {evidenceProjectionWarning([lineage.evidence_projection, ...additionalLineagePages.map(page => page.evidence_projection)]) && <p className="error" role="status">{evidenceProjectionWarning([lineage.evidence_projection, ...additionalLineagePages.map(page => page.evidence_projection)])}</p>}
              {!lineage.trace_spans_available && <p className="muted">Collector delivery is not verified. Durable records remain authoritative; recorded span references are diagnostic only.</p>}
              <details>
                <summary>Recorded span references (incident and outcome records)</summary>
                {[...lineage.trace_refs, ...additionalLineagePages.flatMap(page => page.trace_refs)].length === 0
                  ? <p className="muted">No recorded references in the displayed pages.</p>
                  : [...lineage.trace_refs, ...additionalLineagePages.flatMap(page => page.trace_refs)].map(ref =>
                    <div className="trace-node" key={ref.node_id}>
                      <strong>{ref.node_id}</strong><code>Trace {ref.trace_id}</code><code>Span {ref.span_id}</code>
                    </div>)}
                <p className="muted">Other or missing links are not inferred. A reference does not establish recovery or collector retention.</p>
              </details>
              {lineage.truncated_sources.length > 0 && <>
                <p className="error">Incomplete forensic graph: over 50 records in {lineage.truncated_sources.join(", ")}. Do not treat this view as complete.</p>
                <p className="muted">Only the initial records and the current additional page per kind are retained. Loading the next page replaces the previous additional page.</p>
                {lineage.truncated_sources.map(kind => {
                  const page = lineagePages.incidentId === incident.id
                    ? lineagePages.pages[kind] : undefined;
                  return <button type="button" className="secondary" key={kind}
                    disabled={busy || page?.next_cursor === null}
                    onClick={() => loadMoreLineage(kind)}>
                    {page?.next_cursor === null ? "End of " + kind + " records" : "Next " + kind + " page"}
                  </button>;
                })}
              </>}
              {lineage.links_truncated &&
                <p className="error">The initial graph exceeds the evidence-reference limit. Some links were omitted; this view is incomplete.</p>}
              {lineage.missing_refs.length > 0 &&
                <p className="error">{lineage.missing_refs.length} missing durable link(s). Do not infer the omitted evidence.</p>}
              {additionalLineagePages.some(page => page.links_truncated) &&
                <p className="error">One or more record pages have over 200 references. Some links were intentionally omitted; this is not a complete trace.</p>}
              {additionalLineagePages.some(page => page.missing_refs.length > 0) &&
                <p className="error">Additional record pages contain {additionalLineagePages.reduce((n, page) => n + page.missing_refs.length, 0)} out-of-scope or missing references.</p>}
              <div className="trace-scroll">
                {[...lineage.nodes, ...additionalLineagePages.flatMap(page => page.nodes)].map((node, index) => {
                  const recordId = node.id.split(":")[1];
                  return <div className="trace-node" key={node.id}>
                    <strong>{node.kind}{index >= lineage.nodes.length && " (additional)"}</strong><span>{node.status}</span>
                    <code>{recordId}</code>
                    {node.kind !== "incident" && recordId &&
                      <button type="button" className="secondary" disabled={recordBusy}
                        aria-label={`Inspect ${node.kind} record ${recordId}`}
                        onClick={() => void inspectLineageNode(node.kind, recordId)}>
                        Inspect record
                      </button>}
                    {node.id === "outcome:" + activeInvestigation?.recovery?.latest_outcome_id &&
                      <a href="#investigation-outcome">View latest outcome record</a>}
                  </div>;
                })}
              </div>
              {recordError && <p role="alert" className="error">{recordError}</p>}
              {selectedRecord && selectedRecord.scope === authScope && selectedRecord.incidentId === incident.id &&
                <div className="audit-item" aria-live="polite">
                  <h4>Source record: {selectedRecord.record.id}</h4>
                  <p className="muted">
                    Authority: {selectedRecord.record.authority_kind === "MODEL_OR_INVESTIGATION_CLAIM"
                      ? "Model or investigation claim only — not an independently verified outcome"
                      : selectedRecord.record.authority_kind === "INDEPENDENT_OUTCOME"
                        ? "Independently witnessed outcome"
                        : selectedRecord.record.authority_kind.replaceAll("_", " ")}
                    {" · "}Status: {selectedRecord.record.status}
                  </p>
                  {selectedRecord.record.unresolved_or_failed &&
                    <p role="status" className="error">Unresolved, adverse, or non-verified record state — review authoritative details before acting.</p>}
                  <dl>
                    {Object.entries(selectedRecord.record.details).map(([field, value]) => (
                      <div key={field}>
                        <dt>{field.replaceAll("_", " ")}</dt>
                        <dd className="mono">{typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "none")}</dd>
                      </div>
                    ))}
                  </dl>
                </div>}
              <h3>Causal links ({allLineageEdges.length})</h3>
              {allLineageEdges.length > 80 &&
                <p className="muted">Showing 80 of {allLineageEdges.length} links. Page records remain available above.</p>}
              <div className="trace-scroll">
                {allLineageEdges.slice(0, 80).map((edge, index) =>
                  <div className="trace-node" key={edge.from + edge.to + edge.relation + index}>
                    <strong>{edge.relation.replaceAll("_", " ")}</strong>
                    <code>{edge.from} → {edge.to}</code>
                  </div>
                )}
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
      <section className="incident-history" aria-label="Tenant queue health">
        <div className="history-header">
          <h2>Queue health</h2>
          <button type="button" className="secondary"
            disabled={!authenticated || queueBusy} onClick={() => void loadQueueHealth()}>
            {queueBusy ? "Checking queues..." : "Refresh queue health"}
          </button>
        </div>
        <p className="muted">Durable, tenant-scoped snapshot. This is not a multi-host capacity certification.</p>
        {queueError && <p role="alert" className="error">{queueError}</p>}
        {queueHealth && <div aria-live="polite">
          <p><strong>{queueHealth.active_incidents}</strong> active incidents · <strong>{queueHealth.pending_approvals}</strong> pending approvals · <strong>{queueHealth.pending_outbox}</strong> pending outbox intents · <strong>{queueHealth.unresolved_effects}</strong> unresolved effects</p>
          <p className="muted">Pending approval age p95: {queueHealth.pending_approval_age_p95_seconds}s · Oldest pending approval: {queueHealth.oldest_pending_approval_seconds}s</p>
          <p className="muted">Pending age p95: {queueHealth.pending_age_p95_seconds}s · Oldest pending: {queueHealth.oldest_pending_seconds}s · Oldest active phase: {queueHealth.oldest_active_phase_seconds}s</p>
          <p className="muted">Maximum pending delivery attempts: {queueHealth.max_pending_attempts} · Stuck threshold: {queueHealth.stuck_after_seconds}s</p>
          {queueHealth.experience_dead_letters_unreviewed > 0 &&
            <p role="alert" className="error">
              {queueHealth.experience_dead_letters_unreviewed} experience projection dead letter(s) need operator review. These are excluded from the pending queue.
            </p>}
          {(queueHealth.aged_active_incidents > 0 || queueHealth.aged_pending_approvals > 0 || queueHealth.aged_pending_outbox > 0 || queueHealth.aged_unresolved_effects > 0) &&
            <p role="alert" className="error">
              Backlog requires review: {queueHealth.aged_active_incidents} aged active incidents, {queueHealth.aged_pending_approvals} aged pending approvals, {queueHealth.aged_pending_outbox} aged outbox intents and {queueHealth.aged_unresolved_effects} aged unresolved effects. Inspect receipts and worker health before admitting more work.
            </p>}
        </div>}
      </section>
      <section className="incident-history" aria-label="Experience projection dead-letter review">
        <div className="history-header">
          <h2>Experience projection review</h2>
          <button type="button" className="secondary"
            disabled={!authenticated || deadLetterBusy}
            onClick={() => void loadDeadLetters()}>
            {deadLetterBusy ? "Loading..." : "Load dead letters"}
          </button>
        </div>
        <p className="muted">
          Review and redrive are separate actions. Redrive requires an independent
          security administrator and creates one new local-only projection intent.
          Neither action changes infrastructure or promotes an AI policy.
        </p>
        {canRedrive &&
          <label>
            Redrive remediation reason
            <select value={redriveReason} disabled={deadLetterBusy}
              onChange={event => setRedriveReason(
                event.target.value as "STORE_RESTORED" | "TRANSIENT_FIX_VERIFIED"
              )}>
              <option value="STORE_RESTORED">Storage restored</option>
              <option value="TRANSIENT_FIX_VERIFIED">Transient issue independently verified fixed</option>
            </select>
          </label>}
        {deadLetterError && <p className="error" role="alert">{deadLetterError}</p>}
        {deadLetterPage && <div aria-live="polite">
          {deadLetterPage.records.length === 0 &&
            <p className="muted">No experience dead letters for this tenant.</p>}
          {deadLetterPage.records.map(record =>
            <div className="audit-item" key={record.id}>
              <strong>Projection #{record.id} — {record.code.replaceAll("_", " ")}</strong>
              <span>Incident {record.incident_id} · {record.attempts} attempt(s)</span>
              <span>{record.reviewed ? "Reviewed" : "Needs review"}</span>
              {record.redriven_to !== null &&
                <span>Redrive queued as projection #{record.redriven_to}</span>}
              {canOperate && !record.reviewed &&
                <button type="button" className="secondary"
                  aria-label={"Acknowledge review for projection " + record.id}
                  disabled={deadLetterBusy}
                  onClick={() => void acknowledgeDeadLetter(record)}>
                  Acknowledge review
                </button>}
              {canRedrive && record.reviewed &&
                record.code === "RETRY_EXHAUSTED" &&
                record.redriven_to === null &&
                <button type="button" className="secondary"
                  aria-label={"Authorize single redrive for projection " + record.id}
                  disabled={deadLetterBusy}
                  onClick={() => void redriveDeadLetter(record)}>
                  Authorize single redrive
                </button>}
            </div>
          )}
          {deadLetterPage.next_cursor !== null &&
            <button type="button" className="secondary"
              disabled={deadLetterBusy}
              onClick={() => void loadDeadLetters(true)}>
              Load more dead letters
            </button>}
        </div>}
      </section>
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
