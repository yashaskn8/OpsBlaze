import { useState, useEffect, useCallback } from 'react'
import type {
  ExperienceCapsuleResponse,
  ConsentStatusResponse,
  OutcomeSummaryResponse,
} from '../types/stage7'

interface ExperienceViewProps {
  tenantId: string
  incidentId?: string
}

export function ExperienceView({ tenantId, incidentId }: ExperienceViewProps) {
  const [capsules, setCapsules] = useState<ExperienceCapsuleResponse[]>([])
  const [outcomeSummary, setOutcomeSummary] = useState<OutcomeSummaryResponse | null>(null)
  const [consent, setConsent] = useState<ConsentStatusResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [disputeReason, setDisputeReason] = useState('')
  const [isDisputing, setIsDisputing] = useState(false)
  const [activeSubTab, setActiveSubTab] = useState<'capsules' | 'outcomes' | 'consent'>('capsules')

  const fetchData = useCallback(async () => {
    if (!tenantId) return
    setLoading(true)
    setError(null)
    try {
      const headers = {
        Accept: 'application/json',
        'X-Tenant-ID': tenantId,
      }

      // Fetch consent
      const consentRes = await fetch('/v1/experience/consent', { headers })
      if (consentRes.ok) {
        const consentData = (await consentRes.json()) as ConsentStatusResponse
        setConsent(consentData)
      }

      // Fetch capsules
      const capsUrl = incidentId
        ? `/v1/experience/capsules?incident_id=${encodeURIComponent(incidentId)}`
        : '/v1/experience/capsules?limit=20'
      const capsRes = await fetch(capsUrl, { headers })
      if (capsRes.ok) {
        const capsData = (await capsRes.json()) as { items: ExperienceCapsuleResponse[] }
        setCapsules(capsData.items ?? [])
      }

      // Fetch incident outcome if incidentId is present
      if (incidentId) {
        const outRes = await fetch(`/v1/incidents/${encodeURIComponent(incidentId)}/outcome`, { headers })
        if (outRes.ok) {
          const outData = (await outRes.json()) as OutcomeSummaryResponse
          setOutcomeSummary(outData)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch experience and outcome data')
    } finally {
      setLoading(false)
    }
  }, [tenantId, incidentId])

  useEffect(() => {
    void fetchData()
  }, [fetchData])

  const handleDispute = async () => {
    if (!incidentId || !outcomeSummary?.current_revision || !disputeReason.trim()) return
    try {
      const res = await fetch(`/v1/incidents/${encodeURIComponent(incidentId)}/outcome/feedback`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Tenant-ID': tenantId,
        },
        body: JSON.stringify({
          label: 'DISPUTED',
          reason: disputeReason,
          actor_id: 'operator:sre-lead@opsblaze.internal',
          actor_role: 'INCIDENT_COMMANDER',
          expected_revision_no: outcomeSummary.current_revision.revision_no,
        }),
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: Failed to submit dispute`)
      }
      setIsDisputing(false)
      setDisputeReason('')
      void fetchData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Dispute submission failed')
    }
  }

  const handleToggleConsent = async () => {
    if (!consent) return
    try {
      const res = await fetch('/v1/experience/consent', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Tenant-ID': tenantId,
        },
        body: JSON.stringify({
          reuse_allowed: !consent.reuse_allowed,
          reason: consent.reuse_allowed ? 'Operator privacy policy update' : 'Operator consent granted',
          actor_id: 'operator:privacy-lead',
          actor_role: 'platform_admin',
        }),
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: Failed to update consent`)
      }
      void fetchData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Consent update failed')
    }
  }

  return (
    <div className="experience-view" style={{ padding: '16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h2>E1 Experience & E2 Outcome Adjudication</h2>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            type="button"
            className={activeSubTab === 'capsules' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('capsules')}
          >
            Capsules ({capsules.length})
          </button>
          <button
            type="button"
            className={activeSubTab === 'outcomes' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('outcomes')}
          >
            Outcomes {outcomeSummary?.current_revision ? `(r${outcomeSummary.current_revision.revision_no})` : ''}
          </button>
          <button
            type="button"
            className={activeSubTab === 'consent' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('consent')}
          >
            Consent Policy
          </button>
          <button type="button" className="b1-btn-secondary" onClick={() => void fetchData()}>
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="b1-error-banner" style={{ marginBottom: '12px' }}>{error}</div>}
      {loading && <p>Loading experience records...</p>}

      {activeSubTab === 'consent' && consent && (
        <div className="b1-card" style={{ padding: '16px', background: '#1e293b', borderRadius: '8px' }}>
          <h3>Tenant Consent Epoch Status</h3>
          <p>
            Current Epoch: <strong>v{consent.current_epoch}</strong>
          </p>
          <p>
            Cross-Incident Learning Reuse Allowed:{' '}
            <strong style={{ color: consent.reuse_allowed ? '#4ade80' : '#f87171' }}>
              {consent.reuse_allowed ? 'ALLOWED (Active)' : 'REVOKED (Locked)'}
            </strong>
          </p>
          <p>Last Updated: {new Date(consent.updated_at).toLocaleString()}</p>
          <button
            type="button"
            className={consent.reuse_allowed ? 'b1-btn-danger' : 'b1-btn-primary'}
            style={{ marginTop: '12px' }}
            onClick={() => void handleToggleConsent()}
          >
            {consent.reuse_allowed ? 'Revoke Consent Epoch' : 'Grant Consent Epoch'}
          </button>
        </div>
      )}

      {activeSubTab === 'capsules' && (
        <div className="b1-card" style={{ overflowX: 'auto' }}>
          <h3>Captured Experience Capsules (E1)</h3>
          {capsules.length === 0 ? (
            <p className="b1-empty">No experience capsules captured yet.</p>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #334155' }}>
                  <th style={{ padding: '8px' }}>Capsule ID</th>
                  <th style={{ padding: '8px' }}>Incident ID</th>
                  <th style={{ padding: '8px' }}>Epoch</th>
                  <th style={{ padding: '8px' }}>Classification</th>
                  <th style={{ padding: '8px' }}>Lifecycle</th>
                  <th style={{ padding: '8px' }}>Redaction Digest</th>
                  <th style={{ padding: '8px' }}>Created</th>
                </tr>
              </thead>
              <tbody>
                {capsules.map((c) => (
                  <tr key={c.id} style={{ borderBottom: '1px solid #1e293b' }}>
                    <td style={{ padding: '8px' }}><code>{c.id.slice(0, 8)}...</code></td>
                    <td style={{ padding: '8px' }}><code>{c.incident_id.slice(0, 8)}...</code></td>
                    <td style={{ padding: '8px' }}>v{c.consent_epoch}</td>
                    <td style={{ padding: '8px' }}>
                      <span className="b1-badge">{c.classification}</span>
                    </td>
                    <td style={{ padding: '8px' }}>{c.lifecycle_state}</td>
                    <td style={{ padding: '8px' }}><code>{c.redacted_digest.slice(0, 12)}...</code></td>
                    <td style={{ padding: '8px' }}>{new Date(c.created_at).toLocaleTimeString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {activeSubTab === 'outcomes' && (
        <div className="b1-card" style={{ overflowX: 'auto' }}>
          <h3>Adjudicated Incident Outcomes (E2)</h3>
          {!outcomeSummary || !outcomeSummary.current_revision ? (
            <p className="b1-empty">
              {incidentId
                ? 'No adjudicated outcome record for this active incident.'
                : 'Select or admit an incident to inspect its adjudicated outcome.'}
            </p>
          ) : (
            <div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px', padding: '12px', background: '#0f172a', borderRadius: '6px' }}>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Incident ID:</span>
                  <div><code>{outcomeSummary.incident_id}</code></div>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Revision:</span>
                  <div><strong>r{outcomeSummary.current_revision.revision_no}</strong> (Rule: {outcomeSummary.current_revision.rule_version})</div>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Ground Truth Label:</span>
                  <div>
                    <span className={`b1-badge ${outcomeSummary.current_revision.label === 'VERIFIED_SUCCESS' ? 'b1-status-VERIFIED' : 'b1-status-FAILED'}`}>
                      {outcomeSummary.current_revision.label}
                    </span>
                  </div>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Confidence Band:</span>
                  <div>{outcomeSummary.current_revision.confidence_band}</div>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Dispute Status:</span>
                  <div>
                    {outcomeSummary.is_disputed ? (
                      <span style={{ color: '#f87171', fontWeight: 600 }}>DISPUTED</span>
                    ) : (
                      <span style={{ color: '#4ade80', fontWeight: 600 }}>UNDISPUTED</span>
                    )}
                  </div>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '0.8rem' }}>Observations Count:</span>
                  <div>{outcomeSummary.total_observations}</div>
                </div>
              </div>

              {!outcomeSummary.is_disputed && (
                <div style={{ marginTop: '16px' }}>
                  {!isDisputing ? (
                    <button
                      type="button"
                      className="b1-btn-secondary"
                      onClick={() => setIsDisputing(true)}
                    >
                      Dispute Outcome Label
                    </button>
                  ) : (
                    <div style={{ padding: '12px', background: '#0f172a', borderRadius: '6px' }}>
                      <h4>Dispute Outcome Adjudication</h4>
                      <input
                        type="text"
                        placeholder="Reason for disputing ground truth label..."
                        value={disputeReason}
                        onChange={(e) => setDisputeReason(e.target.value)}
                        style={{ width: '100%', padding: '8px', marginBottom: '8px', borderRadius: '4px', border: '1px solid #334155' }}
                      />
                      <div style={{ display: 'flex', gap: '8px' }}>
                        <button
                          type="button"
                          className="b1-btn-danger"
                          onClick={() => void handleDispute()}
                          disabled={!disputeReason.trim()}
                        >
                          Submit Formal Dispute
                        </button>
                        <button
                          type="button"
                          className="b1-btn-secondary"
                          onClick={() => {
                            setIsDisputing(false)
                            setDisputeReason('')
                          }}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* OBSERVATIONS TABLE */}
              {outcomeSummary.observations.length > 0 && (
                <div style={{ marginTop: '16px' }}>
                  <h4>Supporting Ground Truth Observations ({outcomeSummary.observations.length})</h4>
                  <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.85rem' }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #334155' }}>
                        <th style={{ padding: '6px' }}>Obs ID</th>
                        <th style={{ padding: '6px' }}>Source Kind</th>
                        <th style={{ padding: '6px' }}>Reference</th>
                        <th style={{ padding: '6px' }}>Observed At</th>
                        <th style={{ padding: '6px' }}>Digest</th>
                      </tr>
                    </thead>
                    <tbody>
                      {outcomeSummary.observations.map((obs) => (
                        <tr key={obs.id} style={{ borderBottom: '1px solid #1e293b' }}>
                          <td style={{ padding: '6px' }}><code>{obs.id.slice(0, 8)}...</code></td>
                          <td style={{ padding: '6px' }}>{obs.source_kind}</td>
                          <td style={{ padding: '6px' }}>{obs.source_reference}</td>
                          <td style={{ padding: '6px' }}>{new Date(obs.observed_at).toLocaleTimeString()}</td>
                          <td style={{ padding: '6px' }}><code>{obs.source_digest.slice(0, 10)}...</code></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
