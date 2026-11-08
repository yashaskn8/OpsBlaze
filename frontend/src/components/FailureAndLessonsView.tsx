import { useState, useEffect, useCallback } from 'react'
import type {
  FailureCaseResponse,
  FailureClusterResponse,
  ImprovementProposalResponse,
  OperationalLessonResponse,
  LessonCitation,
  ReviewDecision,
} from '../types/stage8'

interface FailureAndLessonsViewProps {
  tenantId: string
  incidentId?: string
  authToken?: string
}

export function FailureAndLessonsView({ tenantId, incidentId, authToken }: FailureAndLessonsViewProps) {
  const [activeSubTab, setActiveSubTab] = useState<'failures' | 'proposals' | 'lessons' | 'search'>('failures')
  const [failures, setFailures] = useState<FailureCaseResponse[]>([])
  const [selectedCluster, setSelectedCluster] = useState<FailureClusterResponse | null>(null)
  const [lessons, setLessons] = useState<OperationalLessonResponse[]>([])
  const [searchResults, setSearchResults] = useState<LessonCitation[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  const getHeaders = useCallback((contentType?: string) => {
    const h: Record<string, string> = { 'X-Tenant-ID': tenantId }
    if (contentType) h['Content-Type'] = contentType
    else h['Accept'] = 'application/json'
    if (authToken) h['Authorization'] = `Bearer ${authToken}`
    return h
  }, [tenantId, authToken])

  // Cluster quarantine state
  const [quarantineClusterId, setQuarantineClusterId] = useState('')
  const [quarantineReason, setQuarantineReason] = useState('')

  // Proposal review state
  const [reviewProposalId, setReviewProposalId] = useState<string | null>(null)
  const [reviewerId, setReviewerId] = useState('reviewer-alice')
  const [reviewerRole, setReviewerRole] = useState<'INCIDENT_COMMANDER' | 'PLATFORM_ENGINEER' | 'EVALUATION_OWNER'>('INCIDENT_COMMANDER')
  const [reviewDecision, setReviewDecision] = useState<ReviewDecision>('APPROVED')
  const [reviewComments, setReviewComments] = useState('')

  // Lesson validation state
  const [validatingLessonId, setValidatingLessonId] = useState<string | null>(null)
  const [validatorName, setValidatorName] = useState('validator-bob')

  const fetchFailures = useCallback(async () => {
    if (!tenantId) return
    setLoading(true)
    setError(null)
    try {
      const url = incidentId
        ? `/v1/failures?incident_id=${encodeURIComponent(incidentId)}`
        : '/v1/failures'
      const res = await fetch(url, {
        headers: getHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to load failure cases`)
      const data = (await res.json()) as FailureCaseResponse[]
      setFailures(data ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading failure cases')
    } finally {
      setLoading(false)
    }
  }, [tenantId, incidentId, getHeaders])

  const fetchLessons = useCallback(async () => {
    if (!tenantId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/v1/lessons', {
        headers: getHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to load lessons`)
      const data = (await res.json()) as OperationalLessonResponse[]
      setLessons(data ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading lessons')
    } finally {
      setLoading(false)
    }
  }, [tenantId, getHeaders])

  useEffect(() => {
    if (activeSubTab === 'failures') {
      void fetchFailures()
    } else if (activeSubTab === 'lessons') {
      void fetchLessons()
    }
  }, [activeSubTab, fetchFailures, fetchLessons])

  const handleInspectCluster = async (clusterId: string) => {
    setError(null)
    try {
      const res = await fetch(`/v1/failure-clusters/${encodeURIComponent(clusterId)}`, {
        headers: getHeaders(),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to fetch cluster`)
      const data = (await res.json()) as FailureClusterResponse
      setSelectedCluster(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error fetching cluster')
    }
  }

  const handleQuarantineCluster = async () => {
    if (!quarantineClusterId || !quarantineReason.trim()) return
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/failure-clusters/${encodeURIComponent(quarantineClusterId)}/quarantine`, {
        method: 'POST',
        headers: getHeaders('application/json'),
        body: JSON.stringify({ reason: quarantineReason }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to quarantine cluster`)
      setSuccessMsg(`Cluster ${quarantineClusterId} quarantined successfully`)
      setQuarantineClusterId('')
      setQuarantineReason('')
      void fetchFailures()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error quarantining cluster')
    }
  }

  const handleReviewProposal = async () => {
    if (!reviewProposalId) return
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/improvements/${encodeURIComponent(reviewProposalId)}/review`, {
        method: 'POST',
        headers: getHeaders('application/json'),
        body: JSON.stringify({
          reviewer_id: reviewerId,
          reviewer_role: reviewerRole,
          decision: reviewDecision,
          comments: reviewComments,
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to submit proposal review`)
      const updated = (await res.json()) as ImprovementProposalResponse
      setSuccessMsg(`Proposal ${updated.id} reviewed with decision: ${updated.review_decision}`)
      setReviewProposalId(null)
      setReviewComments('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error reviewing proposal')
    }
  }

  const handleValidateLesson = async (lessonId: string) => {
    if (!validatorName.trim()) return
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/lessons/${encodeURIComponent(lessonId)}/validate`, {
        method: 'POST',
        headers: getHeaders('application/json'),
        body: JSON.stringify({ validator: validatorName }),
      })
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string }
        throw new Error(body.detail || `HTTP ${res.status}: Validation rejected`)
      }
      setSuccessMsg(`Lesson ${lessonId} successfully validated & activated!`)
      setValidatingLessonId(null)
      void fetchLessons()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error validating lesson')
    }
  }

  const handleRevokeLesson = async (lessonId: string) => {
    setError(null)
    setSuccessMsg(null)
    try {
      const res = await fetch(`/v1/lessons/${encodeURIComponent(lessonId)}/revoke`, {
        method: 'POST',
        headers: getHeaders('application/json'),
        body: JSON.stringify({ reason: 'Operator revocation via UI' }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Failed to revoke lesson`)
      setSuccessMsg(`Lesson ${lessonId} revoked`)
      void fetchLessons()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error revoking lesson')
    }
  }

  const handleSearchLessons = async () => {
    if (!searchQuery.trim()) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/v1/lessons/search', {
        method: 'POST',
        headers: getHeaders('application/json'),
        body: JSON.stringify({
          query_text: searchQuery,
          limit: 10,
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}: Search failed`)
      const data = (await res.json()) as LessonCitation[]
      setSearchResults(data ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error searching lessons')
    } finally {
      setLoading(false)
    }
  }

  const renderBadge = (status: string) => {
    let bg = '#64748b'
    if (status === 'ACTIVE' || status === 'VALIDATED' || status === 'APPROVED') bg = '#16a34a'
    else if (status === 'QUARANTINED' || status === 'REVOKED' || status === 'REJECTED') bg = '#dc2626'
    else if (status === 'DRAFT' || status === 'UNCERTAIN') bg = '#f59e0b'
    return (
      <span style={{ padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 600, color: '#fff', backgroundColor: bg }}>
        {status}
      </span>
    )
  }

  return (
    <div className="b1-card" style={{ marginTop: '16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid #e2e8f0', paddingBottom: '12px' }}>
        <div>
          <h2 style={{ margin: 0, fontSize: '1.25rem', color: '#0f172a' }}>
            E3 & E4 — Failure Taxonomy & Validated Lesson Registry
          </h2>
          <p style={{ margin: '4px 0 0 0', color: '#64748b', fontSize: '0.875rem' }}>
            Review classified failure cases, quarantine clusters, approve improvement proposals, and manage validated operational memory.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            type="button"
            className={activeSubTab === 'failures' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('failures')}
          >
            E3 Failure Cases
          </button>
          <button
            type="button"
            className={activeSubTab === 'proposals' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('proposals')}
          >
            E3 Proposals
          </button>
          <button
            type="button"
            className={activeSubTab === 'lessons' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('lessons')}
          >
            E4 Lesson Registry
          </button>
          <button
            type="button"
            className={activeSubTab === 'search' ? 'b1-btn-primary' : 'b1-btn-secondary'}
            onClick={() => setActiveSubTab('search')}
          >
            E4 Memory Search
          </button>
        </div>
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

      {/* SUBTAB 1: FAILURES & CLUSTERS */}
      {activeSubTab === 'failures' && (
        <div style={{ marginTop: '16px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ margin: 0, fontSize: '1.05rem', color: '#334155' }}>
              Classified Incident Failure Cases ({failures.length})
            </h3>
            <button type="button" className="b1-btn-secondary" onClick={() => void fetchFailures()} disabled={loading}>
              {loading ? 'Refreshing...' : 'Refresh Failures'}
            </button>
          </div>

          {failures.length === 0 ? (
            <p style={{ color: '#64748b', marginTop: '12px' }}>No failure cases recorded for this tenant/incident.</p>
          ) : (
            <div style={{ overflowX: 'auto', marginTop: '12px' }}>
              <table className="b1-table" style={{ width: '100%', fontSize: '0.85rem' }}>
                <thead>
                  <tr>
                    <th>Case ID</th>
                    <th>Incident ID</th>
                    <th>Subsystem</th>
                    <th>Origin Code</th>
                    <th>First Divergent Step</th>
                    <th>Status</th>
                    <th>Cluster</th>
                  </tr>
                </thead>
                <tbody>
                  {failures.map((fc) => (
                    <tr key={fc.id}>
                      <td><code>{fc.id.slice(0, 8)}...</code></td>
                      <td><code>{fc.incident_id.slice(0, 8)}...</code></td>
                      <td>{fc.subsystem_version}</td>
                      <td><code>{fc.failure_origin_code}</code></td>
                      <td>
                        <span style={{ fontWeight: 600, color: '#dc2626' }}>
                          {fc.first_divergent_step_category}
                        </span>
                        {fc.first_divergent_step && (
                          <div style={{ fontSize: '0.75rem', color: '#64748b' }}>{fc.first_divergent_step}</div>
                        )}
                      </td>
                      <td>{renderBadge(fc.classification_status)}</td>
                      <td>
                        {fc.cluster_id ? (
                          <button
                            type="button"
                            className="b1-btn-secondary"
                            style={{ padding: '2px 6px', fontSize: '11px' }}
                            onClick={() => void handleInspectCluster(fc.cluster_id!)}
                          >
                            Inspect {fc.cluster_id.slice(0, 6)}...
                          </button>
                        ) : (
                          <span style={{ color: '#94a3b8' }}>Unclustered</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Cluster Details Modal / Panel */}
          {selectedCluster && (
            <div style={{ marginTop: '20px', padding: '16px', backgroundColor: '#f8fafc', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h4 style={{ margin: 0, color: '#0f172a' }}>
                  Failure Cluster: <code>{selectedCluster.id}</code> (v{selectedCluster.version})
                </h4>
                <button type="button" className="b1-btn-secondary" onClick={() => setSelectedCluster(null)}>
                  Close
                </button>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginTop: '12px', fontSize: '0.85rem' }}>
                <div><strong>Signature Hash:</strong> <code>{selectedCluster.signature_hash}</code></div>
                <div><strong>Origin Code:</strong> <code>{selectedCluster.failure_origin_code}</code></div>
                <div><strong>Subsystem Version:</strong> {selectedCluster.subsystem_version}</div>
                <div><strong>Case Count:</strong> {selectedCluster.case_count}</div>
                <div><strong>Status:</strong> {renderBadge(selectedCluster.cluster_status)}</div>
                <div><strong>Exemplar Case:</strong> {selectedCluster.exemplar_case_id ? <code>{selectedCluster.exemplar_case_id}</code> : 'None'}</div>
              </div>

              {selectedCluster.cluster_status === 'ACTIVE' && (
                <div style={{ marginTop: '16px', borderTop: '1px solid #e2e8f0', paddingTop: '12px' }}>
                  <h5 style={{ margin: '0 0 8px 0', color: '#b91c1c' }}>Quarantine Cluster</h5>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <input
                      type="text"
                      placeholder="Quarantine reason / justification"
                      value={quarantineReason}
                      onChange={(e) => {
                        setQuarantineReason(e.target.value)
                        setQuarantineClusterId(selectedCluster.id)
                      }}
                      style={{ flex: 1, padding: '6px 10px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                    />
                    <button
                      type="button"
                      className="b1-btn-danger"
                      onClick={() => void handleQuarantineCluster()}
                      disabled={!quarantineReason.trim()}
                    >
                      Quarantine
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* SUBTAB 2: PROPOSALS */}
      {activeSubTab === 'proposals' && (
        <div style={{ marginTop: '16px' }}>
          <h3 style={{ margin: 0, fontSize: '1.05rem', color: '#334155' }}>
            Improvement Proposals & Human Review Gate
          </h3>
          <p style={{ color: '#64748b', fontSize: '0.85rem', margin: '4px 0 12px 0' }}>
            Proposals modify prompt instructions, tool descriptions, or context budgets. Models may draft; deterministic backends and authorized human roles gate evaluation.
          </p>

          <div style={{ padding: '16px', backgroundColor: '#f8fafc', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
            <h4 style={{ margin: '0 0 8px 0', color: '#0f172a' }}>Review Pending Proposal</h4>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px', fontSize: '0.85rem' }}>
              <label>
                Proposal ID:
                <input
                  type="text"
                  placeholder="UUID of proposal"
                  value={reviewProposalId ?? ''}
                  onChange={(e) => setReviewProposalId(e.target.value)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                />
              </label>
              <label>
                Reviewer ID:
                <input
                  type="text"
                  value={reviewerId}
                  onChange={(e) => setReviewerId(e.target.value)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                />
              </label>
              <label>
                Authorized Role:
                <select
                  value={reviewerRole}
                  onChange={(e) => setReviewerRole(e.target.value as typeof reviewerRole)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                >
                  <option value="INCIDENT_COMMANDER">INCIDENT_COMMANDER</option>
                  <option value="PLATFORM_ENGINEER">PLATFORM_ENGINEER</option>
                  <option value="EVALUATION_OWNER">EVALUATION_OWNER</option>
                </select>
              </label>
              <label>
                Decision:
                <select
                  value={reviewDecision}
                  onChange={(e) => setReviewDecision(e.target.value as ReviewDecision)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                >
                  <option value="APPROVED">APPROVED (Ready for E5 Evaluation)</option>
                  <option value="REJECTED">REJECTED</option>
                  <option value="CHANGES_REQUESTED">CHANGES_REQUESTED</option>
                </select>
              </label>
            </div>
            <div style={{ marginTop: '12px' }}>
              <label style={{ fontSize: '0.85rem' }}>
                Review Comments:
                <textarea
                  rows={2}
                  value={reviewComments}
                  onChange={(e) => setReviewComments(e.target.value)}
                  placeholder="Optional review rationale or modification requirements..."
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
                />
              </label>
            </div>
            <button
              type="button"
              className="b1-btn-primary"
              style={{ marginTop: '12px' }}
              onClick={() => void handleReviewProposal()}
              disabled={!reviewProposalId}
            >
              Submit Human Review Verdict
            </button>
          </div>
        </div>
      )}

      {/* SUBTAB 3: LESSON REGISTRY */}
      {activeSubTab === 'lessons' && (
        <div style={{ marginTop: '16px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <h3 style={{ margin: 0, fontSize: '1.05rem', color: '#334155' }}>
                Validated Operational Lessons ({lessons.length})
              </h3>
              <p style={{ margin: '2px 0 0 0', color: '#64748b', fontSize: '0.85rem' }}>
                Dual-key validated lessons with consent-epoch bound and automated TTL expiry.
              </p>
            </div>
            <button type="button" className="b1-btn-secondary" onClick={() => void fetchLessons()} disabled={loading}>
              {loading ? 'Refreshing...' : 'Refresh Lessons'}
            </button>
          </div>

          {lessons.length === 0 ? (
            <p style={{ color: '#64748b', marginTop: '12px' }}>No operational lessons registered.</p>
          ) : (
            <div style={{ overflowX: 'auto', marginTop: '12px' }}>
              <table className="b1-table" style={{ width: '100%', fontSize: '0.85rem' }}>
                <thead>
                  <tr>
                    <th>Lesson ID</th>
                    <th>Type</th>
                    <th>Title & Redacted Summary</th>
                    <th>Confidence</th>
                    <th>Epoch</th>
                    <th>Lifecycle</th>
                    <th>Validation / Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {lessons.map((ls) => (
                    <tr key={ls.id}>
                      <td>
                        <code>{ls.lesson_id}</code>
                        <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>v{ls.version}</div>
                      </td>
                      <td>
                        <span style={{ fontWeight: 600, color: '#4338ca' }}>{ls.lesson_type}</span>
                      </td>
                      <td>
                        <strong>{ls.title}</strong>
                        <div style={{ fontSize: '0.75rem', color: '#475569', marginTop: '2px' }}>
                          {ls.redacted_summary}
                        </div>
                      </td>
                      <td>
                        <span style={{
                          padding: '2px 6px',
                          borderRadius: '4px',
                          fontSize: '11px',
                          fontWeight: 600,
                          backgroundColor: ls.confidence_band === 'HIGH' ? '#dcfce7' : '#fef9c3',
                          color: ls.confidence_band === 'HIGH' ? '#166534' : '#854d0e',
                        }}>
                          {ls.confidence_band}
                        </span>
                      </td>
                      <td>Epoch {ls.consent_epoch}</td>
                      <td>{renderBadge(ls.lifecycle_state)}</td>
                      <td>
                        {ls.lifecycle_state === 'SOURCE_CHECKED' && (
                          validatingLessonId === ls.lesson_id ? (
                            <div style={{ display: 'flex', gap: '4px' }}>
                              <input
                                type="text"
                                value={validatorName}
                                onChange={(e) => setValidatorName(e.target.value)}
                                placeholder="Validator ID"
                                style={{ width: '100px', fontSize: '11px', padding: '2px 4px' }}
                              />
                              <button
                                type="button"
                                className="b1-btn-primary"
                                style={{ padding: '2px 6px', fontSize: '11px' }}
                                onClick={() => void handleValidateLesson(ls.lesson_id)}
                              >
                                Confirm
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              className="b1-btn-primary"
                              style={{ padding: '2px 6px', fontSize: '11px' }}
                              onClick={() => {
                                setValidatingLessonId(ls.lesson_id)
                                setValidatorName('validator-bob')
                              }}
                            >
                              Validate (Dual-Key)
                            </button>
                          )
                        )}
                        {ls.lifecycle_state === 'ACTIVE' && (
                          <button
                            type="button"
                            className="b1-btn-danger"
                            style={{ padding: '2px 6px', fontSize: '11px' }}
                            onClick={() => void handleRevokeLesson(ls.lesson_id)}
                          >
                            Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* SUBTAB 4: SEARCH SIMULATION */}
      {activeSubTab === 'search' && (
        <div style={{ marginTop: '16px' }}>
          <h3 style={{ margin: 0, fontSize: '1.05rem', color: '#334155' }}>
            Operational Memory Search & Citations
          </h3>
          <p style={{ color: '#64748b', fontSize: '0.85rem', margin: '4px 0 12px 0' }}>
            Simulate investigator full-text retrieval with consent-epoch filtering and cryptographic attribution digests.
          </p>

          <div style={{ display: 'flex', gap: '8px' }}>
            <input
              type="text"
              placeholder="Enter incident context or error tokens (e.g. replica lag deadlock connection pool)..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              style={{ flex: 1, padding: '8px 12px', borderRadius: '6px', border: '1px solid #cbd5e1' }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void handleSearchLessons()
              }}
            />
            <button
              type="button"
              className="b1-btn-primary"
              onClick={() => void handleSearchLessons()}
              disabled={loading || !searchQuery.trim()}
            >
              {loading ? 'Searching...' : 'Search Lessons'}
            </button>
          </div>

          {searchResults.length > 0 && (
            <div style={{ marginTop: '16px' }}>
              <h4 style={{ margin: '0 0 8px 0', color: '#0f172a' }}>Retrieved Citations ({searchResults.length})</h4>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {searchResults.map((cit, idx) => (
                  <div key={idx} style={{ padding: '12px', backgroundColor: '#f8fafc', borderRadius: '6px', border: '1px solid #e2e8f0' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 600, color: '#1e293b' }}>{cit.title}</span>
                      <span style={{ fontSize: '0.75rem', color: '#64748b' }}>
                        Citation Digest: <code>{cit.retrieval_digest.slice(0, 12)}...</code>
                      </span>
                    </div>
                    <p style={{ margin: '4px 0', fontSize: '0.85rem', color: '#334155' }}>
                      {cit.sanitized_summary}
                    </p>
                    <div style={{ fontSize: '0.75rem', color: '#64748b', display: 'flex', gap: '12px' }}>
                      <span>Type: <strong>{cit.lesson_type}</strong></span>
                      <span>Confidence: <strong>{cit.confidence_band}</strong></span>
                      <span>Outcome Ref: <code>{cit.source_outcome_revision_id.slice(0, 8)}...</code></span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
