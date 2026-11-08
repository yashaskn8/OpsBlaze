import { useCallback, useEffect, useState, type FormEvent } from 'react'
import './App.css'
import { StaleLockoutBanner } from './components/StaleLockoutBanner'
import { WorkbenchView } from './components/WorkbenchView'
import { ApprovalCockpitView } from './components/ApprovalCockpitView'
import { TraceExplorerView } from './components/TraceExplorerView'
import type { ApprovalResponse } from './types/stage6'

export type IncidentStatus =
  | 'ACCEPTED'
  | 'DISPATCH_PENDING'
  | 'INVESTIGATING'
  | 'AWAITING_APPROVAL'
  | 'EXECUTING'
  | 'VERIFYING'
  | 'VERIFIED'
  | 'FAILED'
  | 'NEEDS_REVIEW'
  | 'CANCELLED'

export type SeverityLevel = 'SEV1' | 'SEV2' | 'SEV3' | 'SEV4'

export interface InvestigationObservation {
  probe_id: string
  target_id: string
  finding_code: string
  summary: string
  read_only: true
  deterministic_digest: string
}

export interface InvestigationResult {
  incident_id: string
  tenant_id: string
  target_id: string
  hypothesis: string
  recommended_status: IncidentStatus
  requires_human_approval: true
  observations: InvestigationObservation[]
}

export interface IncidentResponse {
  id: string
  tenant_id: string
  client_key: string
  target_id: string
  request_sha256: string
  status: IncidentStatus
  status_version: number
  workflow_id: string
  created_at: string
  updated_at: string
  outbox_delivered: boolean
  outbox_attempts: number
  projection_lag_ms: number
  projection_stale: boolean
  investigation: InvestigationResult | null
}

const DEFAULT_TENANT_ID = '11111111-1111-4111-8111-111111111111'
const STORAGE_INCIDENT_ID_KEY = 'opsblaze.b1.active_incident_id'
const STORAGE_TENANT_ID_KEY = 'opsblaze.b1.tenant_id'

const CANCELLABLE_STATES = new Set<IncidentStatus>([
  'ACCEPTED',
  'DISPATCH_PENDING',
  'INVESTIGATING',
  'AWAITING_APPROVAL',
])

function generateIdempotencyKey(): string {
  const rand = Math.random().toString(36).substring(2, 10)
  return `b1-key-${Date.now()}-${rand}`
}

function getInitialIncidentId(): string {
  const params = new URLSearchParams(window.location.search)
  return (
    params.get('incident_id') ??
    window.sessionStorage.getItem(STORAGE_INCIDENT_ID_KEY) ??
    ''
  )
}

function getInitialTenantId(): string {
  return (
    window.sessionStorage.getItem(STORAGE_TENANT_ID_KEY) ?? DEFAULT_TENANT_ID
  )
}

export function App() {
  const [tenantId, setTenantId] = useState<string>(getInitialTenantId)
  const [targetId, setTargetId] = useState<string>('sim-db-primary')
  const [title, setTitle] = useState<string>('Elevated replica lag on primary')
  const [severity, setSeverity] = useState<SeverityLevel>('SEV2')
  const [idempotencyKey, setIdempotencyKey] = useState<string>(
    generateIdempotencyKey
  )

  const [lookupIncidentId, setLookupIncidentId] = useState<string>(
    getInitialIncidentId
  )
  const [incident, setIncident] = useState<IncidentResponse | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  // Stage 6 Tabs & Lockout State
  const [activeTab, setActiveTab] = useState<'workbench' | 'approval' | 'trace' | 'admission'>('workbench')
  const [activeApproval, setActiveApproval] = useState<ApprovalResponse | null>(null)
  const [isLockedOut, setIsLockedOut] = useState<boolean>(false)
  const [pingLagSeconds, setPingLagSeconds] = useState<number>(0)

  const persistActiveContext = useCallback(
    (incId: string, tId: string) => {
      window.sessionStorage.setItem(STORAGE_INCIDENT_ID_KEY, incId)
      window.sessionStorage.setItem(STORAGE_TENANT_ID_KEY, tId)
      const url = new URL(window.location.href)
      if (incId) {
        url.searchParams.set('incident_id', incId)
      } else {
        url.searchParams.delete('incident_id')
      }
      window.history.replaceState({}, '', url.toString())
    },
    []
  )

  const fetchIncidentSnapshot = useCallback(
    async (idToFetch: string, activeTenantId: string) => {
      const trimmed = idToFetch.trim()
      if (!trimmed) {
        return
      }
      setLoading(true)
      setErrorMessage(null)
      try {
        const res = await fetch(`/v1/incidents/${encodeURIComponent(trimmed)}`, {
          method: 'GET',
          headers: {
            Accept: 'application/json',
            'X-Tenant-ID': activeTenantId.trim(),
          },
        })
        if (!res.ok) {
          const errBody = (await res.json().catch(() => ({}))) as {
            detail?: unknown
          }
          const detailText =
            typeof errBody.detail === 'string'
              ? errBody.detail
              : JSON.stringify(errBody.detail ?? `HTTP ${res.status}`)
          throw new Error(`HTTP ${res.status}: ${detailText}`)
        }
        const data = (await res.json()) as IncidentResponse
        setIncident(data)
        setLookupIncidentId(data.id)
        persistActiveContext(data.id, activeTenantId.trim())
      } catch (err) {
        const msg =
          err instanceof Error ? err.message : 'Failed to load incident snapshot'
        setErrorMessage(msg)
      } finally {
        setLoading(false)
      }
    },
    [persistActiveContext]
  )

  // Recover authoritative backend snapshot after browser reload if an incident ID was active
  useEffect(() => {
    const initialId = getInitialIncidentId()
    const initialTenant = getInitialTenantId()
    if (!initialId) {
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch(
          `/v1/incidents/${encodeURIComponent(initialId.trim())}`,
          {
            method: 'GET',
            headers: {
              Accept: 'application/json',
              'X-Tenant-ID': initialTenant.trim(),
            },
          }
        )
        if (!res.ok) {
          return
        }
        const data = (await res.json()) as IncidentResponse
        if (!cancelled) {
          setIncident(data)
        }
      } catch {
        // Ignore initial reload probe error until operator interacts
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const handleAdmitIncident = async (e: FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setErrorMessage(null)
    try {
      const res = await fetch('/v1/incidents', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'X-Tenant-ID': tenantId.trim(),
          'Idempotency-Key': idempotencyKey.trim(),
        },
        body: JSON.stringify({
          target_id: targetId.trim(),
          severity,
          title: title.trim(),
        }),
      })

      if (!res.ok) {
        const errBody = (await res.json().catch(() => ({}))) as {
          detail?: unknown
        }
        const detailText =
          typeof errBody.detail === 'string'
            ? errBody.detail
            : JSON.stringify(errBody.detail ?? `HTTP ${res.status}`)
        throw new Error(`Admission failed (HTTP ${res.status}): ${detailText}`)
      }

      const data = (await res.json()) as IncidentResponse
      setIncident(data)
      setLookupIncidentId(data.id)
      persistActiveContext(data.id, tenantId.trim())
      setIdempotencyKey(generateIdempotencyKey())
      setActiveTab('workbench')
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Unknown admission error'
      setErrorMessage(msg)
    } finally {
      setLoading(false)
    }
  }

  const handleCancelIncident = async () => {
    if (!incident) {
      return
    }
    setLoading(true)
    setErrorMessage(null)
    try {
      const res = await fetch(
        `/v1/incidents/${encodeURIComponent(incident.id)}/cancel`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json',
            'X-Tenant-ID': tenantId.trim(),
          },
          body: JSON.stringify({
            expected_version: incident.status_version,
            reason: 'Operator cancelled from UI workbench',
          }),
        }
      )

      if (!res.ok) {
        const errBody = (await res.json().catch(() => ({}))) as {
          detail?: unknown
        }
        const detailText =
          typeof errBody.detail === 'string'
            ? errBody.detail
            : JSON.stringify(errBody.detail ?? `HTTP ${res.status}`)
        throw new Error(
          `Cancellation failed (HTTP ${res.status}): ${detailText}`
        )
      }

      const updated = (await res.json()) as IncidentResponse
      setIncident(updated)
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : 'Cancellation request failed'
      setErrorMessage(msg)
    } finally {
      setLoading(false)
    }
  }

  const canCancel =
    incident !== null &&
    CANCELLABLE_STATES.has(incident.status) &&
    !loading

  const activeIncidentId = incident?.id || lookupIncidentId.trim()

  return (
    <main className="b1-app">
      <header className="b1-header" style={{ borderBottom: '1px solid #e2e8f0', paddingBottom: '12px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <h1>OpsBlaze / TITAN-20</h1>
            <p className="b1-subtitle" style={{ margin: '4px 0 0 0' }}>
              Operator Control Plane, Approval Cockpit &amp; Investigation Workbench
            </p>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <label style={{ fontSize: '11px', color: '#64748b' }}>Tenant:</label>
            <input
              type="text"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              style={{ fontSize: '11px', padding: '4px 8px', width: '260px', fontFamily: 'monospace' }}
            />
          </div>
        </div>

        {/* Global Incident Lookup Bar */}
        <div style={{ display: 'flex', gap: '8px', marginTop: '12px', alignItems: 'center' }}>
          <input
            type="text"
            placeholder="Active Incident UUID..."
            value={lookupIncidentId}
            onChange={(e) => setLookupIncidentId(e.target.value)}
            style={{ flex: 1, padding: '6px 10px', fontSize: '12px', fontFamily: 'monospace' }}
          />
          <button
            type="button"
            className="b1-btn-secondary"
            onClick={() => void fetchIncidentSnapshot(lookupIncidentId, tenantId)}
            disabled={loading || !lookupIncidentId.trim()}
          >
            {loading ? 'Loading...' : 'Load Incident'}
          </button>
        </div>

        {/* Navigation Tabs */}
        <nav style={{ display: 'flex', gap: '8px', marginTop: '14px' }}>
          <button
            type="button"
            onClick={() => setActiveTab('workbench')}
            style={{
              padding: '8px 16px',
              borderRadius: '6px',
              border: activeTab === 'workbench' ? '2px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: activeTab === 'workbench' ? '#eff6ff' : '#ffffff',
              color: activeTab === 'workbench' ? '#1d4ed8' : '#334155',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            P1 — Live Workbench
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('approval')}
            style={{
              padding: '8px 16px',
              borderRadius: '6px',
              border: activeTab === 'approval' ? '2px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: activeTab === 'approval' ? '#eff6ff' : '#ffffff',
              color: activeTab === 'approval' ? '#1d4ed8' : '#334155',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            P2 — Approval Cockpit
            {activeApproval?.state === 'PENDING' && (
              <span style={{ padding: '2px 6px', borderRadius: '10px', fontSize: '10px', backgroundColor: '#f59e0b', color: '#ffffff' }}>
                ACTION REQUIRED
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('trace')}
            style={{
              padding: '8px 16px',
              borderRadius: '6px',
              border: activeTab === 'trace' ? '2px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: activeTab === 'trace' ? '#eff6ff' : '#ffffff',
              color: activeTab === 'trace' ? '#1d4ed8' : '#334155',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            P3 — Trace Explorer
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('admission')}
            style={{
              padding: '8px 16px',
              borderRadius: '6px',
              border: activeTab === 'admission' ? '2px solid #2563eb' : '1px solid #cbd5e1',
              backgroundColor: activeTab === 'admission' ? '#eff6ff' : '#ffffff',
              color: activeTab === 'admission' ? '#1d4ed8' : '#334155',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            B1 — Admission Lab
          </button>
        </nav>
      </header>

      {/* Stale Lockout Banner */}
      <StaleLockoutBanner pingLagSeconds={pingLagSeconds} isDisconnected={isLockedOut} />

      {errorMessage && (
        <div className="b1-alert b1-alert-error" role="alert" style={{ margin: '16px 0' }}>
          <strong>API Error:</strong> {errorMessage}
        </div>
      )}

      {/* TAB CONTENT */}
      <div style={{ marginTop: '16px' }}>
        {activeTab === 'workbench' && (
          activeIncidentId ? (
            <WorkbenchView
              incidentId={activeIncidentId}
              tenantId={tenantId}
              onActiveApprovalChange={setActiveApproval}
              onLockoutChange={(locked, lag) => {
                setIsLockedOut(locked)
                setPingLagSeconds(lag)
              }}
            />
          ) : (
            <p style={{ padding: '20px', color: '#64748b' }}>
              No incident active. Admit a new incident or load an incident UUID to open the live workbench.
            </p>
          )
        )}

        {activeTab === 'approval' && (
          <ApprovalCockpitView
            incidentId={activeIncidentId}
            tenantId={tenantId}
            activeApproval={activeApproval}
            isLockedOut={isLockedOut}
            onRefresh={() => void fetchIncidentSnapshot(activeIncidentId, tenantId)}
          />
        )}

        {activeTab === 'trace' && (
          activeIncidentId ? (
            <TraceExplorerView incidentId={activeIncidentId} tenantId={tenantId} />
          ) : (
            <p style={{ padding: '20px', color: '#64748b' }}>
              No incident active. Load an incident UUID to view its causal DAG trace.
            </p>
          )
        )}

        {activeTab === 'admission' && (
          <div className="b1-grid">
            <section className="b1-card" aria-labelledby="admit-heading">
              <h2 id="admit-heading">Admit Incident (POST /v1/incidents)</h2>
              <form onSubmit={(e) => void handleAdmitIncident(e)} className="b1-form">
                <label htmlFor="idempotency-key-input">
                  Idempotency-Key
                  <div className="b1-inline-input">
                    <input
                      id="idempotency-key-input"
                      type="text"
                      value={idempotencyKey}
                      onChange={(e) => setIdempotencyKey(e.target.value)}
                      required
                    />
                    <button
                      type="button"
                      className="b1-btn-secondary"
                      onClick={() => setIdempotencyKey(generateIdempotencyKey())}
                    >
                      New Key
                    </button>
                  </div>
                </label>

                <label htmlFor="target-id-input">
                  Target Resource ID
                  <input
                    id="target-id-input"
                    type="text"
                    value={targetId}
                    onChange={(e) => setTargetId(e.target.value)}
                    required
                  />
                </label>

                <label htmlFor="severity-select">
                  Severity
                  <select
                    id="severity-select"
                    value={severity}
                    onChange={(e) => setSeverity(e.target.value as SeverityLevel)}
                  >
                    <option value="SEV1">SEV1</option>
                    <option value="SEV2">SEV2</option>
                    <option value="SEV3">SEV3</option>
                    <option value="SEV4">SEV4</option>
                  </select>
                </label>

                <label htmlFor="title-input">
                  Summary / Title
                  <input
                    id="title-input"
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </label>

                <button type="submit" className="b1-btn-primary" disabled={loading}>
                  {loading ? 'Submitting...' : 'Admit Durable Incident'}
                </button>
              </form>
            </section>

            <section className="b1-card" aria-labelledby="status-heading">
              <h2 id="status-heading">Authoritative Incident Raw Snapshot</h2>
              {!incident ? (
                <p className="b1-empty">No incident loaded.</p>
              ) : (
                <div className="b1-snapshot">
                  <dl className="b1-dl">
                    <div>
                      <dt>Incident ID</dt>
                      <dd><code>{incident.id}</code></dd>
                    </div>
                    <div>
                      <dt>Status Projection</dt>
                      <dd>
                        <span className={`b1-badge b1-status-${incident.status}`}>
                          {incident.status}
                        </span>{' '}
                        (version <strong>v{incident.status_version}</strong>)
                      </dd>
                    </div>
                    <div>
                      <dt>Target ID</dt>
                      <dd><code>{incident.target_id}</code></dd>
                    </div>
                    <div>
                      <dt>Created / Updated</dt>
                      <dd>{new Date(incident.created_at).toISOString()} / {new Date(incident.updated_at).toISOString()}</dd>
                    </div>
                  </dl>

                  <div className="b1-actions" style={{ marginTop: '16px' }}>
                    <button
                      type="button"
                      className="b1-btn-danger"
                      disabled={!canCancel}
                      onClick={() => void handleCancelIncident()}
                    >
                      Cancel Incident (v{incident.status_version})
                    </button>
                  </div>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </main>
  )
}

export default App
