import { useState, useEffect, useCallback } from 'react'
import type {
  CandidateManifestResponse,
  ManifestDetailResponse,
  CandidateTrialResponse,
  ReviewDecision,
  CandidateProvenanceType,
} from '../types/stage9'
import { evaluationsApi, EvaluationsApiError } from '../services/evaluationsApi'

interface EvaluationsViewProps {
  tenantId: string
  incidentId?: string
  authToken?: string
  authenticatedUser?: {
    actor_id: string
    actor_kind: string
    roles: string[]
  } | null
}

export function EvaluationsView({
  tenantId,
  incidentId: _incidentId,
  authToken: propAuthToken,
  authenticatedUser,
}: EvaluationsViewProps) {
  const [manifests, setManifests] = useState<CandidateManifestResponse[]>([])
  const [selectedManifestId, setSelectedManifestId] = useState<string | null>(null)
  const [manifestDetail, setManifestDetail] = useState<ManifestDetailResponse | null>(null)
  const [trials, setTrials] = useState<CandidateTrialResponse[]>([])
  const [loading, setLoading] = useState(false)
  const [runningEval, setRunningEval] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflictError, setConflictError] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  // Auth token state (for authenticated review actions)
  const [authToken, setAuthToken] = useState<string>(() => propAuthToken || localStorage.getItem('opsblaze.auth_token') || '')
  const [principal, setPrincipal] = useState<{
    actor_id: string
    actor_kind: string
    roles: string[]
    can_review_evaluation: boolean
    can_approve_release: boolean
  } | null>(null)

  useEffect(() => {
    if (propAuthToken) {
      setAuthToken(propAuthToken)
    }
  }, [propAuthToken])

  useEffect(() => {
    if (authenticatedUser) {
      setPrincipal({
        actor_id: authenticatedUser.actor_id,
        actor_kind: authenticatedUser.actor_kind,
        roles: authenticatedUser.roles,
        can_review_evaluation:
          authenticatedUser.actor_kind === 'human' &&
          authenticatedUser.roles.some(r => ['evaluation_owner', 'incident_commander'].includes(r)),
        can_approve_release:
          authenticatedUser.actor_kind === 'human' &&
          authenticatedUser.roles.some(r => ['release_owner', 'incident_commander'].includes(r)),
      })
    }
  }, [authenticatedUser])

  // Manifest Registration state
  const [showRegisterForm, setShowRegisterForm] = useState(false)
  const [regTitle, setRegTitle] = useState('')
  const [regProvenance, setRegProvenance] = useState<CandidateProvenanceType>('E3_PROPOSAL')
  const [regProposalId, setRegProposalId] = useState('')
  const [regLessonIds, setRegLessonIds] = useState('')
  const [regTargetComponent, setRegTargetComponent] = useState('investigator_a1_prompt')
  const [regOriginalText, setRegOriginalText] = useState('')
  const [regProposedText, setRegProposedText] = useState('')
  const [regSampleSize, setRegSampleSize] = useState(10)

  // Review state
  const [reviewDecision, setReviewDecision] = useState<ReviewDecision>('ACCEPTED')
  const [reviewComments, setReviewComments] = useState('')

  const fetchPrincipal = useCallback(async () => {
    if (!authToken) {
      setPrincipal(null)
      return
    }
    const p = await evaluationsApi.getAuthenticatedPrincipal(tenantId, authToken)
    setPrincipal(p)
  }, [tenantId, authToken])

  const fetchManifests = useCallback(async () => {
    if (!tenantId) return
    setLoading(true)
    setError(null)
    setConflictError(null)
    try {
      const data = await evaluationsApi.listManifests(tenantId, authToken)
      setManifests(data ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading candidate manifests')
    } finally {
      setLoading(false)
    }
  }, [tenantId, authToken])

  const fetchManifestDetail = useCallback(async (id: string) => {
    setLoading(true)
    setError(null)
    setConflictError(null)
    try {
      const data = await evaluationsApi.getManifestDetail(id, tenantId, authToken)
      setManifestDetail(data)

      const trialsData = await evaluationsApi.getTrials(id, tenantId, authToken)
      setTrials(trialsData ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error loading manifest details')
    } finally {
      setLoading(false)
    }
  }, [tenantId, authToken])

  useEffect(() => {
    void fetchManifests()
    void fetchPrincipal()
  }, [fetchManifests, fetchPrincipal])

  useEffect(() => {
    if (selectedManifestId) {
      void fetchManifestDetail(selectedManifestId)
    } else {
      setManifestDetail(null)
      setTrials([])
    }
  }, [selectedManifestId, fetchManifestDetail])

  const handleRegisterManifest = async () => {
    if (!regTitle.trim()) return
    setError(null)
    setConflictError(null)
    setSuccessMsg(null)
    try {
      const reqPayload = {
        title: regTitle.trim(),
        incumbent_bundle: {
          policy_version: 'v1.0',
          tool_catalog_version: 'v1',
        },
        candidate_bundle: {
          base_policy_version: 'v1.0',
          target_component: regTargetComponent,
          proposal_diff: {
            template_name: 'prompt_v1',
            section_name: 'rules',
            original_text: regOriginalText,
            proposed_text: regProposedText,
          },
          active_lesson_ids: regLessonIds ? regLessonIds.split(',').map((s) => s.trim()) : undefined,
        },
        candidate_provenance_type: regProvenance,
        source_proposal_id: regProposalId.trim() || undefined,
        source_lesson_ids: regLessonIds ? regLessonIds.split(',').map((s) => s.trim()) : undefined,
        fixture_set_digest: 'e'.repeat(64),
        holdout_set_digest: 'f'.repeat(64),
        sample_size_plan: regSampleSize,
        auto_freeze: false,
      }
      const created = await evaluationsApi.createManifest(reqPayload, tenantId, authToken)
      setSuccessMsg(`Evaluation manifest '${created.title}' successfully registered (ID: ${created.id})`)
      setShowRegisterForm(false)
      setRegTitle('')
      setRegProposalId('')
      setRegLessonIds('')
      void fetchManifests()
      setSelectedManifestId(created.id)
    } catch (err) {
      if (err instanceof EvaluationsApiError && err.status === 409) {
        setConflictError(`Registration Conflict: ${err.message}`)
      } else {
        setError(err instanceof Error ? err.message : 'Failed to register evaluation manifest')
      }
    }
  }

  const handleFreezeManifest = async (manifest: CandidateManifestResponse) => {
    setError(null)
    setConflictError(null)
    setSuccessMsg(null)
    try {
      await evaluationsApi.freezeManifest(manifest.id, manifest.version, tenantId, authToken)
      setSuccessMsg(`Manifest ${manifest.id} frozen successfully (version v${manifest.version + 1})`)
      void fetchManifests()
      if (selectedManifestId === manifest.id) void fetchManifestDetail(manifest.id)
    } catch (err) {
      if (err instanceof EvaluationsApiError && err.status === 409) {
        setConflictError(`Version Conflict: Manifest state or version has changed. Please refresh and retry. (${err.message})`)
      } else {
        setError(err instanceof Error ? err.message : 'Error freezing manifest')
      }
    }
  }

  const handleRunEvaluation = async (id: string) => {
    setRunningEval(true)
    setError(null)
    setConflictError(null)
    setSuccessMsg(null)
    try {
      const report = await evaluationsApi.runEvaluation(id, 'eval-runner-ui', 120, tenantId, authToken)
      setSuccessMsg(`Evaluation run completed. Gate Verdict: ${report.report_status}`)
      void fetchManifests()
      void fetchManifestDetail(id)
    } catch (err) {
      if (err instanceof EvaluationsApiError && err.status === 409) {
        setConflictError(`Execution Conflict: ${err.message}`)
      } else {
        setError(err instanceof Error ? err.message : 'Error executing evaluation run')
      }
    } finally {
      setRunningEval(false)
    }
  }

  const handleSignOffReview = async (manifestId: string, expectedVersion: number) => {
    setError(null)
    setConflictError(null)
    setSuccessMsg(null)
    try {
      await evaluationsApi.submitReview(manifestId, expectedVersion, reviewDecision, reviewComments, tenantId, authToken)
      setSuccessMsg(`Evaluation review signed off with decision: ${reviewDecision}`)
      setReviewComments('')
      void fetchManifestDetail(manifestId)
    } catch (err) {
      if (err instanceof EvaluationsApiError && err.status === 409) {
        setConflictError(`Review Conflict (409): ${err.message}. Source evidence may be invalidated or report version changed.`)
      } else if (err instanceof EvaluationsApiError && err.status === 403) {
        setError(`Authorization Denied (403): ${err.message}. Authenticated principal lacks evaluation reviewer authority.`)
      } else if (err instanceof EvaluationsApiError && err.status === 401) {
        setError(`Authentication Required (401): Please configure a valid Bearer token for a human reviewer.`)
      } else {
        setError(err instanceof Error ? err.message : 'Error submitting review sign-off')
      }
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
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            type="button"
            className={showRegisterForm ? 'b1-btn-secondary' : 'b1-btn-primary'}
            onClick={() => setShowRegisterForm(!showRegisterForm)}
          >
            {showRegisterForm ? 'Cancel Registration' : '+ Register Candidate'}
          </button>
          <button type="button" className="b1-btn-secondary" onClick={() => void fetchManifests()} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh Manifests'}
          </button>
        </div>
      </div>

      {/* Authenticated Actor Bar */}
      <div style={{ marginTop: '10px', padding: '8px 12px', backgroundColor: '#f1f5f9', borderRadius: '6px', fontSize: '0.8rem', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <span>Authenticated Principal: </span>
          {principal ? (
            <strong>
              {principal.actor_id} ({principal.roles.join(', ') || 'No Roles'}) —{' '}
              <span style={{ color: principal.can_review_evaluation ? '#16a34a' : '#dc2626' }}>
                {principal.can_review_evaluation ? 'Authorized for E5 Review' : 'Unauthorized for Review'}
              </span>
            </strong>
          ) : (
            <span style={{ color: '#d97706' }}>No active Bearer session (read-only mode)</span>
          )}
        </div>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          <input
            type="password"
            placeholder="Set Bearer token..."
            value={authToken}
            onChange={(e) => {
              setAuthToken(e.target.value)
              localStorage.setItem('opsblaze.auth_token', e.target.value)
            }}
            style={{ fontSize: '11px', padding: '2px 6px', width: '180px' }}
          />
          {authToken && (
            <button
              type="button"
              className="b1-btn-secondary"
              style={{ fontSize: '11px', padding: '2px 6px' }}
              onClick={() => {
                setAuthToken('')
                localStorage.removeItem('opsblaze.auth_token')
                setPrincipal(null)
              }}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="b1-alert b1-alert-error" style={{ marginTop: '12px' }}>
          <strong>API Error:</strong> {error}
        </div>
      )}
      {conflictError && (
        <div className="b1-alert b1-alert-error" style={{ marginTop: '12px', backgroundColor: '#fff1f2', borderColor: '#fecdd3', color: '#9f1239' }}>
          <strong>State Conflict (HTTP 409):</strong> {conflictError}
        </div>
      )}
      {successMsg && (
        <div className="b1-alert b1-alert-success" style={{ marginTop: '12px', backgroundColor: '#dcfce7', color: '#166534', border: '1px solid #86efac' }}>
          <strong>Success:</strong> {successMsg}
        </div>
      )}

      {/* REGISTRATION FORM */}
      {showRegisterForm && (
        <div style={{ marginTop: '16px', padding: '16px', backgroundColor: '#f8fafc', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
          <h3 style={{ margin: '0 0 12px 0', fontSize: '1.05rem', color: '#0f172a' }}>
            Register New Evaluation Candidate Manifest
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px', fontSize: '0.85rem' }}>
            <label>
              Title:
              <input
                type="text"
                placeholder="e.g. Prompt Refinement for Readback Fence"
                value={regTitle}
                onChange={(e) => setRegTitle(e.target.value)}
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              />
            </label>
            <label>
              Provenance Type:
              <select
                value={regProvenance}
                onChange={(e) => setRegProvenance(e.target.value as CandidateProvenanceType)}
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              >
                <option value="E3_PROPOSAL">E3_PROPOSAL (Approved Proposal)</option>
                <option value="E4_LESSON_SET">E4_LESSON_SET (Validated Lessons)</option>
                <option value="COMBINED_OFFLINE_STUDY">COMBINED_OFFLINE_STUDY</option>
              </select>
            </label>
            {regProvenance === 'E3_PROPOSAL' && (
              <label>
                Source Proposal UUID:
                <input
                  type="text"
                  placeholder="UUID of approved proposal"
                  value={regProposalId}
                  onChange={(e) => setRegProposalId(e.target.value)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
                />
              </label>
            )}
            {regProvenance === 'E4_LESSON_SET' && (
              <label>
                Source Lesson IDs (comma-separated):
                <input
                  type="text"
                  placeholder="UUID1, UUID2"
                  value={regLessonIds}
                  onChange={(e) => setRegLessonIds(e.target.value)}
                  style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
                />
              </label>
            )}
            <label>
              Target Component:
              <select
                value={regTargetComponent}
                onChange={(e) => setRegTargetComponent(e.target.value)}
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              >
                <option value="investigator_a1_prompt">investigator_a1_prompt</option>
                <option value="context_window_budget">context_window_budget</option>
                <option value="tool_description">tool_description</option>
              </select>
            </label>
            <label>
              Planned Pairs:
              <input
                type="number"
                min={1}
                max={500}
                value={regSampleSize}
                onChange={(e) => setRegSampleSize(parseInt(e.target.value, 10) || 10)}
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              />
            </label>
          </div>
          <div style={{ marginTop: '12px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', fontSize: '0.85rem' }}>
            <label>
              Original Text:
              <textarea
                rows={2}
                value={regOriginalText}
                onChange={(e) => setRegOriginalText(e.target.value)}
                placeholder="Incumbent text to be replaced..."
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              />
            </label>
            <label>
              Proposed Text:
              <textarea
                rows={2}
                value={regProposedText}
                onChange={(e) => setRegProposedText(e.target.value)}
                placeholder="Candidate replacement text..."
                style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1', marginTop: '4px' }}
              />
            </label>
          </div>
          <div style={{ marginTop: '12px', display: 'flex', gap: '8px' }}>
            <button
              type="button"
              className="b1-btn-primary"
              onClick={() => void handleRegisterManifest()}
              disabled={!regTitle.trim()}
            >
              Submit Candidate Registration
            </button>
            <button
              type="button"
              className="b1-btn-secondary"
              onClick={() => setShowRegisterForm(false)}
            >
              Cancel
            </button>
          </div>
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
                  <th>State (v)</th>
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
                    <td>
                      {renderLifecycleBadge(m.lifecycle_state)}{' '}
                      <span style={{ fontSize: '10px', color: '#64748b' }}>v{m.version}</span>
                    </td>
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
                            onClick={() => void handleFreezeManifest(m)}
                          >
                            Freeze (v{m.version})
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
                ID: <code>{manifestDetail.manifest.id}</code> | SHA-256: <code>{manifestDetail.manifest.manifest_sha256}</code> | Version: v{manifestDetail.manifest.version}
              </div>
            </div>
            {manifestDetail.latest_report && renderReportBadge(manifestDetail.latest_report.report_status)}
          </div>

          {/* STATISTICAL REPORT SUMMARY */}
          {manifestDetail.latest_report && (
            <div style={{ marginTop: '16px', padding: '14px', backgroundColor: '#ffffff', borderRadius: '6px', border: '1px solid #e2e8f0' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h4 style={{ margin: '0 0 10px 0', fontSize: '0.95rem', color: '#1e293b' }}>
                  Statistical Gate Metrics (Paired Oracle Evaluation Report v{manifestDetail.latest_report.version})
                </h4>
                <div>
                  <span style={{ fontSize: '0.8rem', fontWeight: 600, color: manifestDetail.latest_report.promotion_eligible ? '#16a34a' : '#dc2626' }}>
                    Promotion Eligible: {manifestDetail.latest_report.promotion_eligible ? 'YES' : 'NO'}
                  </span>
                  {manifestDetail.latest_report.invalidation_reason && (
                    <div style={{ fontSize: '0.75rem', color: '#dc2626' }}>
                      Invalidation: {manifestDetail.latest_report.invalidation_reason}
                    </div>
                  )}
                </div>
              </div>
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
                  Human Review & Promotion Sign-Off (CAS Version v{manifestDetail.latest_report.version})
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
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '8px', fontSize: '0.85rem' }}>
                    <label>
                      Decision:
                      <select
                        value={reviewDecision}
                        onChange={(e) => setReviewDecision(e.target.value as ReviewDecision)}
                        style={{ width: '100%', padding: '6px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
                      >
                        <option value="ACCEPTED">ACCEPTED (Eligible for Release)</option>
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
                        onClick={() => void handleSignOffReview(manifestDetail.manifest.id, manifestDetail.latest_report!.version)}
                        disabled={!principal?.can_review_evaluation}
                      >
                        {principal?.can_review_evaluation ? 'Sign Off Review' : 'Review Authority Required'}
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
