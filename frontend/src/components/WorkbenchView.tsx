import { useState, useEffect, useRef, useCallback } from 'react'
import type { ApprovalResponse, WorkbenchSnapshotResponse } from '../types/stage6'

interface Props {
  incidentId: string
  tenantId: string
  onActiveApprovalChange: (app: ApprovalResponse | null) => void
  onLockoutChange: (isLocked: boolean, lagSeconds: number) => void
  authToken?: string
}

export function WorkbenchView({
  incidentId,
  tenantId,
  onActiveApprovalChange,
  onLockoutChange,
  authToken,
}: Props) {
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshotResponse | null>(null)
  const [etag, setEtag] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sseConnected, setSseConnected] = useState(false)
  const [lastPingTime, setLastPingTime] = useState<number>(Date.now())
  const [pingLag, setPingLag] = useState<number>(0)

  const etagRef = useRef<string | null>(null)
  etagRef.current = etag

  // 1. Fetch Workbench Snapshot with ETag validation
  const fetchSnapshot = useCallback(async () => {
    if (!incidentId) return
    setLoading(true)
    setError(null)
    try {
      const headers: Record<string, string> = {
        'X-Tenant-ID': tenantId,
      }
      if (authToken) {
        headers['Authorization'] = `Bearer ${authToken}`
      }
      if (etagRef.current) {
        headers['If-None-Match'] = etagRef.current
      }

      const res = await fetch(`/v1/incidents/${encodeURIComponent(incidentId)}/workbench`, {
        headers,
      })

      if (res.status === 304) {
        // Not Modified: current state is still fresh
        setLoading(false)
        return
      }

      if (!res.ok) {
        throw new Error(`Failed to load workbench snapshot: HTTP ${res.status}`)
      }

      const newEtag = res.headers.get('ETag')
      if (newEtag) {
        setEtag(newEtag)
      }
      const data = (await res.json()) as WorkbenchSnapshotResponse
      setSnapshot(data)
      onActiveApprovalChange(data.active_approval)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [incidentId, tenantId, onActiveApprovalChange])

  useEffect(() => {
    void fetchSnapshot()
  }, [fetchSnapshot])

  // 2. Connect SSE Event Stream & Track Heartbeat Lag
  useEffect(() => {
    if (!incidentId) return

    let isMounted = true
    const sseUrl = `/v1/incidents/${encodeURIComponent(incidentId)}/events`
    // Note: Standard browser EventSource cannot send custom headers directly,
    // but in same-origin or proxied lab setups or query params:
    const eventSource = new EventSource(sseUrl)

    eventSource.onopen = () => {
      if (isMounted) {
        setSseConnected(true)
        setLastPingTime(Date.now())
      }
    }

    eventSource.addEventListener('connected', () => {
      if (isMounted) {
        setLastPingTime(Date.now())
      }
    })

    eventSource.addEventListener('ping', () => {
      if (isMounted) {
        setLastPingTime(Date.now())
      }
    })

    eventSource.addEventListener('incident_updated', () => {
      if (isMounted) {
        setLastPingTime(Date.now())
        void fetchSnapshot()
      }
    })

    eventSource.onerror = () => {
      if (isMounted) {
        setSseConnected(false)
      }
    }

    return () => {
      isMounted = false
      eventSource.close()
    }
  }, [incidentId, fetchSnapshot])

  // 3. Monitor ping lag every second and trigger stale lockout if lag > 30s
  useEffect(() => {
    const timer = setInterval(() => {
      const lag = (Date.now() - lastPingTime) / 1000
      setPingLag(lag)
      const isLocked = !sseConnected || lag > 30.0
      onLockoutChange(isLocked, lag)
    }, 1000)

    return () => clearInterval(timer)
  }, [lastPingTime, sseConnected, onLockoutChange])

  if (loading && !snapshot) {
    return <div style={{ padding: '20px' }}>Loading live investigation workbench...</div>
  }

  if (error && !snapshot) {
    return <div style={{ padding: '20px', color: '#dc2626' }}>Error: {error}</div>
  }

  if (!snapshot) {
    return <div style={{ padding: '20px' }}>No incident workbench available.</div>
  }

  return (
    <div className="b1-panel" style={{ padding: '20px', background: '#ffffff', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h3 style={{ margin: 0 }}>P1 — Live Investigation Workbench</h3>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span
            style={{
              padding: '4px 8px',
              borderRadius: '4px',
              fontSize: '11px',
              fontWeight: 600,
              backgroundColor: sseConnected ? '#dcfce7' : '#fee2e2',
              color: sseConnected ? '#166534' : '#991b1b',
            }}
          >
            {sseConnected ? '● SSE Live Stream' : '○ SSE Disconnected'} (Lag: {pingLag.toFixed(1)}s)
          </span>
          <button
            type="button"
            onClick={() => void fetchSnapshot()}
            style={{ padding: '4px 10px', fontSize: '12px', borderRadius: '4px', border: '1px solid #cbd5e1', cursor: 'pointer' }}
          >
            Refresh Snapshot
          </button>
        </div>
      </div>

      {/* Abstention Banner */}
      {snapshot.abstention.abstained && (
        <div style={{ padding: '12px 16px', backgroundColor: '#fef3c7', border: '1px solid #fde047', borderRadius: '6px', marginBottom: '16px', color: '#854d0e' }}>
          <strong>A4 Calibrated Abstention / Escalation:</strong>{' '}
          {snapshot.abstention.reason || 'Investigation halted due to uncertainty or budget constraints.'}
        </div>
      )}

      {/* Budget & Headroom Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px', marginBottom: '20px', padding: '12px', background: '#f8fafc', borderRadius: '6px', border: '1px solid #e2e8f0' }}>
        <div>
          <div style={{ fontSize: '11px', color: '#64748b' }}>Tokens Settled / Reserved</div>
          <div style={{ fontSize: '15px', fontWeight: 600 }}>{snapshot.budget.settled_tokens} / {snapshot.budget.reserved_tokens}</div>
        </div>
        <div>
          <div style={{ fontSize: '11px', color: '#64748b' }}>Probes Executed / Cap</div>
          <div style={{ fontSize: '15px', fontWeight: 600 }}>{snapshot.budget.settled_probes} / {snapshot.budget.max_steps}</div>
        </div>
        <div>
          <div style={{ fontSize: '11px', color: '#64748b' }}>Settled Cost (micros)</div>
          <div style={{ fontSize: '15px', fontWeight: 600 }}>{snapshot.budget.settled_cost_micros} µ$</div>
        </div>
        <div>
          <div style={{ fontSize: '11px', color: '#64748b' }}>Investigation Steps</div>
          <div style={{ fontSize: '15px', fontWeight: 600 }}>{snapshot.steps.length}</div>
        </div>
      </div>

      {/* Hypotheses Section */}
      <div style={{ marginBottom: '24px' }}>
        <h4 style={{ margin: '0 0 10px 0', borderBottom: '1px solid #e2e8f0', paddingBottom: '4px' }}>
          A1 Calibrated Hypotheses ({snapshot.hypotheses.length})
        </h4>
        {snapshot.hypotheses.length === 0 ? (
          <p style={{ color: '#94a3b8', fontSize: '13px' }}>No active hypotheses formed yet.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {snapshot.hypotheses.map((h) => (
              <div
                key={h.hypothesis_id}
                style={{
                  padding: '10px 14px',
                  borderRadius: '6px',
                  border: '1px solid #e2e8f0',
                  background: '#f8fafc',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                }}
              >
                <div>
                  <div style={{ fontWeight: 600, fontSize: '13px' }}>
                    #{h.rank} {h.title}
                  </div>
                  <div style={{ fontSize: '12px', color: '#64748b', marginTop: '2px' }}>
                    {h.rationale}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <span style={{ fontSize: '14px', fontWeight: 'bold', color: '#2563eb' }}>
                    {(h.confidence * 100).toFixed(0)}%
                  </span>
                  <div style={{ fontSize: '10px', color: '#64748b', textTransform: 'uppercase' }}>
                    {h.status}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Evidence Ledger Section */}
      <div style={{ marginBottom: '24px' }}>
        <h4 style={{ margin: '0 0 10px 0', borderBottom: '1px solid #e2e8f0', paddingBottom: '4px' }}>
          A3 Versioned Evidence Ledger ({snapshot.evidence.length})
        </h4>
        {snapshot.evidence.length === 0 ? (
          <p style={{ color: '#94a3b8', fontSize: '13px' }}>No immutable evidence items captured yet.</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {snapshot.evidence.map((ev) => (
              <div
                key={ev.id}
                style={{
                  padding: '10px 14px',
                  borderRadius: '6px',
                  border: '1px solid #e2e8f0',
                  background: '#ffffff',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                  <span style={{ fontWeight: 600, fontSize: '12px' }}>
                    {ev.source} (Target: {ev.target_id}:{ev.target_version})
                  </span>
                  <span style={{ fontSize: '11px', padding: '2px 6px', background: '#e2e8f0', borderRadius: '4px' }}>
                    {ev.classification} • {ev.validity_status}
                  </span>
                </div>
                <div style={{ fontSize: '11px', color: '#64748b', marginBottom: '4px' }}>
                  SHA-256: <code>{ev.sha256_digest}</code>
                </div>
                <pre style={{ margin: 0, padding: '6px 8px', background: '#f1f5f9', borderRadius: '4px', fontSize: '11px', overflowX: 'auto' }}>
                  {JSON.stringify(ev.raw_payload_redacted, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Tool Call Timeline Section */}
      <div>
        <h4 style={{ margin: '0 0 10px 0', borderBottom: '1px solid #e2e8f0', paddingBottom: '4px' }}>
          A2 Diagnostic Tool Calls ({snapshot.tool_calls.length})
        </h4>
        {snapshot.tool_calls.length === 0 ? (
          <p style={{ color: '#94a3b8', fontSize: '13px' }}>No tool calls executed yet.</p>
        ) : (
          <table style={{ width: '100%', fontSize: '12px', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: '#f8fafc', borderBottom: '1px solid #e2e8f0', textAlign: 'left' }}>
                <th style={{ padding: '6px 8px' }}>Step</th>
                <th style={{ padding: '6px 8px' }}>Tool ID</th>
                <th style={{ padding: '6px 8px' }}>Target</th>
                <th style={{ padding: '6px 8px' }}>Status</th>
                <th style={{ padding: '6px 8px' }}>Duration</th>
                <th style={{ padding: '6px 8px' }}>Digest</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.tool_calls.map((tc) => (
                <tr key={tc.id} style={{ borderBottom: '1px solid #f1f5f9' }}>
                  <td style={{ padding: '6px 8px' }}>#{tc.step_no}</td>
                  <td style={{ padding: '6px 8px', fontWeight: 600 }}><code>{tc.tool_id}</code></td>
                  <td style={{ padding: '6px 8px' }}><code>{tc.target_id}</code></td>
                  <td style={{ padding: '6px 8px' }}>
                    <span style={{ color: tc.status === 'COMPLETED' ? '#166534' : '#991b1b' }}>
                      {tc.status}
                    </span>
                  </td>
                  <td style={{ padding: '6px 8px' }}>{tc.duration_ms.toFixed(1)} ms</td>
                  <td style={{ padding: '6px 8px' }}><code style={{ fontSize: '10px' }}>{tc.args_digest.slice(0, 16)}...</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
