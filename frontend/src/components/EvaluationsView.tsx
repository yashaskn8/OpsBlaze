import { useState, useEffect, useCallback } from 'react'
import type {
  CandidateManifestResponse,
  ManifestDetailResponse,
  CandidateTrialResponse,
  CandidateReportResponse,
  ReviewDecision,
} from '../types/stage9'

interface EvaluationsViewProps {
  tenantId: string
  incidentId?: string
}

export function EvaluationsView({ tenantId }: EvaluationsViewProps) {
  const [manifests, setManifests] = useState<CandidateManifestResponse[]>([])
  const [selectedManifestId, setSelectedManifestId] = useState<string | null>(null)
  const [manifestDetail, setManifestDetail] = useState<ManifestDetailResponse | null>(null)
  const [trials, setTrials] = useState<CandidateTrialResponse[]>([])
  const [loading, setLoading] = useState(false)
  const [runningEval, setRunningEval] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  // Review state
  const [reviewerId, setReviewerId] = useState('eval-reviewer-carol')
  const [reviewerRole, setReviewerRole] = useState<'EVALUATION_OWNER' | 'PLATFORM_ENGINEER' | 'INCIDENT_COMMANDER'>('EVALUATION_OWNER')
  const [reviewDecision, setReviewDecision] = useState<ReviewDecision>('ACCEPTED')
  const [reviewComments, setReviewComments] = useState('')

  const fetchManifests = useCallback(async () => {
    if (!tenantId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/v1/evaluations', {
        headers: { Accept: 'application/json', 'X-Tenant-ID': tenantId },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to load candidate manifests`)
      const data = (await res.json()) as CandidateManifestResponse[]
      setManifests(data ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading candidate manifests')
    } finally {
      setLoading(false)
    }
  }, [tenantId])

  const fetchManifestDetail = useCallback(async (id: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}`, {
        headers: { Accept: 'application/json', 'X-Tenant-ID': tenantId },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to load manifest details`)
      const data = (await res.json()) as ManifestDetailResponse
      setManifestDetail(data)

      // Also fetch trials
      const trialsRes = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/trials`, {
        headers: { Accept: 'application/json', 'X-Tenant-ID': tenantId },
      })
      if (trialsRes.ok) {
        const trialsData = (await trialsRes.json()) as CandidateTrialResponse[]
        setTrials(trialsData ?? [])
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading manifest details')
    } finally {
      setLoading(false)
    }
  }, [tenantId])

  useEffect(() => {
    void fetchManifests()
  }, [fetchManifests])

  useEffect(() => {
    if (selectedManifestId) {
      void fetchManifestDetail(selectedManifestId)
    } else {
      setManifestDetail(null)
      setTrials([])
    }
  }, [selectedManifestId, fetchManifestDetail])

  const handleFreezeManifest = async (id: string) => {
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/freeze`, {
        method: 'POST',
        headers: { 'X-Tenant-ID': tenantId },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to freeze manifest`)
      setSuccessMsg(`Manifest ${id} frozen successfully (immutable)`)
      void fetchManifests()
      if (selectedManifestId === id) void fetchManifestDetail(id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error freezing manifest')
    }
  }

  const handleRunEvaluation = async (id: string) => {
    setRunningEval(true)
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/run`, {
        method: 'POST',
        headers: { 'X-Tenant-ID': tenantId },
      })
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail || `HTTP ${res.status}: Evaluation run failed`)
      }
      const report = (await res.json()) as CandidateReportResponse
      setSuccessMsg(`Evaluation completed with verdict: ${report.report_status}`)
      void fetchManifests()
      void fetchManifestDetail(id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error running evaluation')
    } finally {
      setRunningEval(false)
    }
  }

  const handleSignOffReview = async (id: string) => {
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/review`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Tenant-ID': tenantId,
        },
        body: JSON.stringify({
          reviewer_id: reviewerId,
          reviewer_role: reviewerRole,
          decision: reviewDecision,
          comments: reviewComments,
        }),
      })
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail || `HTTP ${res.status}: Review submission failed`)
      }
      setSuccessMsg(`Evaluation review signed off with decision: ${reviewDecision}`)
      setReviewComments('')
      void fetchManifestDetail(id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error submitting review sign-off')
    }
  }

  const renderLifecycleBadge = (state: string) => {
    let bg = '#64748b'
    if (state === 'COMPLETED') bg = '#16a34a'
    else if (state === 'FROZEN') bg = '#2563eb'
    else if (state === 'RUNNING' || state === 'ANALYZING') bg = '#f59e0b'
    else if (state === 'INVALIDATED' || state === 'CANCELLED') bg = '#dc2626'
    return (
      <span style={{ padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 600, color: '#fff', backgroundColor: bg }}>
        {state}
      </span>
    )
  }

  const renderReportBadge = (status: string) => {
    let bg = '#64748b'
    if (status === 'PASS') bg = '#16a34a'
    else if (status === 'FAIL') bg = '#dc2626'
    else if (status === 'INCONCLUSIVE') bg = '#f59e0b'
    return (
      <span style={{ padding: '4px 10px', borderRadius: '4px', fontSize: '12px', fontWeight: 700, color: '#fff', backgroundColor: bg }}>
        GATE VERDICT: {status}
      </span>
    )
  }

  return (
    <div className="b1-card" style={{ marginTop: '16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid #e2e8f0', paddingBottom: '12px' }}>
        <div>
          <h2 style={{ margin: 0, fontSize: '1.25rem', color: '#0f172a' }}>
            S1 & E5 — Candidate-to-Incumbent Regression Gate
          </h2>
          <p style={{ margin: '4px 0 0 0', color: '#64748b', fontSize: '0.875rem' }}>
            Statistically rigorous paired evaluation gate testing candidate policy/lessons against pinned incumbent baselines.
          </p>
        </div>
        <button type="button" className="b1-btn-secondary" onClick={() => void fetchManifests()} disabled={loading}>
          {loading ? 'Refreshing...' : 'Refresh Manifests'}
        </button>
      </div>

      {error && (
        <div className="b1-alert b1-alert-error" style={{ marginTop: '12px' }}>
          <strong>Error:</strong> {error}
        </div>
      )}
      {successMsg && (
        <div className="b1-alert b1-alert-success" style={{ marginTop: '12px', backgroundColor: '#dcfce7', color: '#166534', border: '1px solid #86efac' }}>
          <strong>Success:</strong> {successMsg}
        </div>
      )}

      {/* MANIFESTS LIST */}
      <div style={{ marginTop: '16px' }}>
        <h3 style={{ margin: '0 0 8px 0', fontSize: '1.05rem', color: '#334155' }}>
          Registered Evaluation Manifests ({manifests.length})
        </h3>
        {manifests.length === 0 ? (
          <p style={{ color: '#64748b' }}>No candidate evaluation manifests registered for this tenant.</p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="b1-table" style={{ width: '100%', fontSize: '0.85rem' }}>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Manifest SHA-256</th>
                  <th>Provenance</th>
                  <th>Model / Subsystem</th>
                  <th>Planned Pairs</th>
                  <th>State</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {manifests.map((m) => (
                  <tr key={m.id} style={{ backgroundColor: selectedManifestId === m.id ? '#eff6ff' : 'transparent' }}>
                    <td><strong>{m.title}</strong></td>
                    <td><code>{m.manifest_sha256.slice(0, 10)}...</code></td>
                    <td>{m.candidate_provenance_type}</td>
                    <td>{m.model_id}</td>
                    <td>{m.sample_size_plan}</td>
                    <td>{renderLifecycleBadge(m.lifecycle_state)}</td>
                    <td>
                      <div style={{ display: 'flex', gap: '6px' }}>
                        <button
                          type="button"
                          className="b1-btn-secondary"
                          style={{ padding: '2px 8px', fontSize: '11px' }}
                          onClick={() => setSelectedManifestId(m.id === selectedManifestId ? null : m.id)}
                        >
                          {selectedManifestId === m.id ? 'Hide' : 'Details'}
                        </button>
                        {m.lifecycle_state === 'REGISTERED' && (
                          <button
                            type="button"
                            className="b1-btn-primary"
                            style={{ padding: '2px 8px', fontSize: '11px' }}
                            onClick={() => void handleFreezeManifest(m.id)}
                          >
                            Freeze
                          </button>
                        )}
                        {m.lifecycle_state === 'FROZEN' && (
                          <button
                            type="button"
                            className="b1-btn-primary"
                            style={{ padding: '2px 8px', fontSize: '11px', backgroundColor: '#7c3aed' }}
                            onClick={() => void handleRunEvaluation(m.id)}
                            disabled={runningEval}
                          >
                            {runningEval ? 'Running...' : 'Execute Evaluation'}
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* SELECTED MANIFEST DETAILS & REPORT */}
      {manifestDetail && (
        <div style={{ marginTop: '24px', padding: '16px', backgroundColor: '#f8fafc', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <h3 style={{ margin: 0, color: '#0f172a' }}>
                Manifest Details: {manifestDetail.manifest.title}
              </h3>
              <div style={{ fontSize: '0.8rem', color: '#64748b', marginTop: '4px' }}>
                ID: <code>{manifestDetail.manifest.id}</code> | SHA-256: <code>{manifestDetail.manifest.manifest_sha256}</code>
              </div>
            </div>
            {manifestDetail.latest_report && renderReportBadge(manifestDetail.latest_report.report_status)}
          </div>

          {/* STATISTICAL REPORT SUMMARY */}
          {manifestDetail.latest_report && (
            <div style={{ marginTop: '16px', padding: '14px', backgroundColor: '#ffffff', borderRadius: '6px', border: '1px solid #e2e8f0' }}>
              <h4 style={{ margin: '0 0 10px 0', fontSize: '0.95rem', color: '#1e293b' }}>
                Statistical Gate Metrics (Paired Oracle Evaluation)
              </h4>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '12px', fontSize: '0.85rem' }}>
                <div>
                  <div style={{ color: '#64748b' }}>Recovery Rate (Incumbent)</div>
                  <strong style={{ fontSize: '1.1rem' }}>
                    {(manifestDetail.latest_report.recovery_rate_incumbent * 100).toFixed(1)}%
                  </strong>
                </div>
                <div>
                  <div style={{ color: '#64748b' }}>Recovery Rate (Candidate)</div>
                  <strong style={{ fontSize: '1.1rem', color: manifestDetail.latest_report.recovery_delta >= 0 ? '#16a34a' : '#dc2626' }}>
                    {(manifestDetail.latest_report.recovery_rate_candidate * 100).toFixed(1)}%
                  </strong>
                </div>
                <div>
                  <div style={{ color: '#64748b' }}>Δ (Newcombe 95% CI)</div>
                  <strong style={{ fontSize: '1rem' }}>
                    {(manifestDetail.latest_report.recovery_delta * 100).toFixed(1)}% [
                    {(manifestDetail.latest_report.recovery_ci_low * 100).toFixed(1)}%,{' '}
                    {(manifestDetail.latest_report.recovery_ci_high * 100).toFixed(1)}%]
                  </strong>
                </div>
                <div>
                  <div style={{ color: '#64748b' }}>McNemar Stat (p-value)</div>
                  <strong>
                    {manifestDetail.latest_report.mcnemar_statistic !== null ? manifestDetail.latest_report.mcnemar_statistic.toFixed(2) : 'N/A'}{' '}
                    (p={manifestDetail.latest_report.mcnemar_p_value !== null ? manifestDetail.latest_report.mcnemar_p_value.toFixed(3) : 'N/A'})
                  </strong>
                </div>
                <div>
                  <div style={{ color: '#64748b' }}>Negative Control Pass Rate</div>
                  <strong style={{ color: manifestDetail.latest_report.negative_control_pass_rate === 1.0 ? '#16a34a' : '#dc2626' }}>
                    {(manifestDetail.latest_report.negative_control_pass_rate * 100).toFixed(1)}%
                  </strong>
                </div>
                <div>
                  <div style={{ color: '#64748b' }}>Unsafe Actions Detected</div>
                  <strong style={{ color: manifestDetail.latest_report.unsafe_action_count === 0 ? '#16a34a' : '#dc2626' }}>
                    {manifestDetail.latest_report.unsafe_action_count}
                  </strong>
                </div>
              </div>

              {/* Gate Verdicts Checklist */}
              {manifestDetail.latest_report.gate_verdicts.length > 0 && (
                <div style={{ marginTop: '14px', borderTop: '1px solid #f1f5f9', paddingTop: '10px' }}>
                  <div style={{ fontSize: '0.8rem', fontWeight: 600, color: '#475569', marginBottom: '6px' }}>
                    Individual Gate Verdicts:
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                    {manifestDetail.latest_report.gate_verdicts.map((gv, idx) => (
                      <span
                        key={idx}
                        style={{
                          padding: '3px 8px',
                          borderRadius: '4px',
                          fontSize: '11px',
                          fontWeight: 600,
                          backgroundColor: gv.status === 'PASS' ? '#dcfce7' : '#fee2e2',
                          color: gv.status === 'PASS' ? '#166534' : '#991b1b',
                          border: `1px solid ${gv.status === 'PASS' ? '#86efac' : '#fca5a5'}`,
                        }}
                      >
                        {gv.gate_name}: {gv.status}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* REVIEW SIGN-OFF SECTION */}
              <div style={{ marginTop: '16px', borderTop: '1px solid #e2e8f0', paddingTop: '12px' }}>
                <h5 style={{ margin: '0 0 8px 0', color: '#0f172a' }}>
                  Human Review & Promotion Sign-Off
                </h5>
                {manifestDetail.latest_report.review_decision ? (
                  <div style={{ fontSize: '0.85rem' }}>
                    Reviewed by <strong>{manifestDetail.latest_report.reviewer_id}</strong> ({manifestDetail.latest_report.reviewer_role}) with verdict{' '}
                    <span style={{ fontWeight: 700, color: manifestDetail.latest_report.review_decision === 'ACCEPTED' ? '#16a34a' : '#dc2626' }}>
                      {manifestDetail.latest_report.review_decision}
                    </span>
                    {manifestDetail.latest_report.review_comments && (
                      <div style={{ color: '#475569', marginTop: '2px' }}>Comments: {manifestDetail.latest_report.review_comments}</div>
                    )}
                  </div>
                ) : (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '8px', fontSize: '0.85rem' }}>
                    <label>
                      Reviewer ID:
                      <input
                        type="text"
                        value={reviewerId}
                        onChange={(e) => setReviewerId(e.target.value)}
                        style={{ width: '100%', padding: '4px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                      />
                    </label>
                    <label>
                      Authorized Role:
                      <select
                        value={reviewerRole}
                        onChange={(e) => setReviewerRole(e.target.value as typeof reviewerRole)}
                        style={{ width: '100%', padding: '4px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                      >
                        <option value="EVALUATION_OWNER">EVALUATION_OWNER</option>
                        <option value="PLATFORM_ENGINEER">PLATFORM_ENGINEER</option>
                        <option value="INCIDENT_COMMANDER">INCIDENT_COMMANDER</option>
                      </select>
                    </label>
                    <label>
                      Decision:
                      <select
                        value={reviewDecision}
                        onChange={(e) => setReviewDecision(e.target.value as ReviewDecision)}
                        style={{ width: '100%', padding: '4px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                      >
                        <option value="ACCEPTED">ACCEPTED</option>
                        <option value="REJECTED">REJECTED</option>
                        <option value="INCONCLUSIVE_ACKNOWLEDGED">INCONCLUSIVE_ACKNOWLEDGED</option>
                      </select>
                    </label>
                    <div style={{ gridColumn: '1 / -1' }}>
                      <input
                        type="text"
                        placeholder="Review comments / rationale..."
                        value={reviewComments}
                        onChange={(e) => setReviewComments(e.target.value)}
                        style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                      />
                    </div>
                    <div>
                      <button
                        type="button"
                        className="b1-btn-primary"
                        style={{ marginTop: '4px' }}
                        onClick={() => void handleSignOffReview(manifestDetail.manifest.id)}
                      >
                        Sign Off Review
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* PAIRED TRIALS LIST */}
          <div style={{ marginTop: '16px' }}>
            <h4 style={{ margin: '0 0 8px 0', fontSize: '0.95rem', color: '#1e293b' }}>
              Paired Execution Trials ({trials.length})
            </h4>
            {trials.length === 0 ? (
              <p style={{ color: '#64748b', fontSize: '0.85rem' }}>No trials executed yet for this manifest.</p>
            ) : (
              <div style={{ maxHeight: '250px', overflowY: 'auto' }}>
                <table className="b1-table" style={{ width: '100%', fontSize: '0.8rem' }}>
                  <thead>
                    <tr>
                      <th>Pair ID</th>
                      <th>Arm</th>
                      <th>Fixture ID</th>
                      <th>Status</th>
                      <th>Correct?</th>
                      <th>Duration</th>
                      <th>Unsafe Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trials.map((t) => (
                      <tr key={t.id}>
                        <td><code>{t.pair_id}</code></td>
                        <td>
                          <span style={{ fontWeight: 600, color: t.arm === 'CANDIDATE' ? '#2563eb' : '#475569' }}>
                            {t.arm}
                          </span>
                        </td>
                        <td>{t.fixture_id} {t.is_negative_control && '(Control)'}</td>
                        <td>{t.trial_status}</td>
                        <td>
                          <span style={{ color: t.decision_correct ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
                            {t.decision_correct ? 'YES' : 'NO'}
                          </span>
                        </td>
                        <td>{t.duration_ms}ms</td>
                        <td>
                          <span style={{ color: t.unsafe_action_detected ? '#dc2626' : '#16a34a' }}>
                            {t.unsafe_action_count}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
