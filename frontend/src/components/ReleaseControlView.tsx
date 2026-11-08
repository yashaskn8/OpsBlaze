/**
 * Feature E6 — Policy Release Control & Automated Rollback View
 * Authentic React 19 TSX Component.
 */

import React, { useEffect, useState, useCallback } from 'react'
import type {
  CanaryMetrics,
  PolicyBundle,
  PolicyRelease,
  ReleaseAuditEvent,
} from '../types/stage11'
import {
  releaseControlApi,
  ReleaseControlApiError,
} from '../services/releaseControlApi'

interface Props {
  tenantId: string
  authToken?: string
  authenticatedUser?: {
    actor_id: string
    actor_kind: string
    roles: string[]
  } | null
}

export const ReleaseControlView: React.FC<Props> = ({
  tenantId,
  authToken,
  authenticatedUser,
}) => {
  const [targetComponent, setTargetComponent] = useState<string>('investigator')
  const [releases, setReleases] = useState<PolicyRelease[]>([])
  const [bundles, setBundles] = useState<PolicyBundle[]>([])
  const [selectedRelease, setSelectedRelease] = useState<PolicyRelease | null>(null)
  const [audits, setAudits] = useState<ReleaseAuditEvent[]>([])
  const [metrics, setMetrics] = useState<CanaryMetrics | null>(null)

  // Loading & error states
  const [isLoading, setIsLoading] = useState<boolean>(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionSuccess, setActionSuccess] = useState<string | null>(null)

  // Form inputs
  const [reviewComments, setReviewComments] = useState<string>('')
  const [canaryPercentage, setCanaryPercentage] = useState<number>(10)
  const [rollbackReason, setRollbackReason] = useState<string>('')
  const [killSwitchJustification, setKillSwitchJustification] = useState<string>('')
  const [showKillSwitchModal, setShowKillSwitchModal] = useState<boolean>(false)

  // New release drafting
  const [selectedCandidateBundleId, setSelectedCandidateBundleId] = useState<string>('')
  const [selectedIncumbentBundleId, setSelectedIncumbentBundleId] = useState<string>('')

  const fetchReleasesAndBundles = useCallback(async () => {
    setIsLoading(true)
    setActionError(null)
    try {
      const [fetchedReleases, fetchedBundles] = await Promise.all([
        releaseControlApi.listReleases(
          tenantId,
          { target_component: targetComponent, limit: 20 },
          authToken
        ),
        releaseControlApi.listBundles(
          tenantId,
          { target_component: targetComponent, limit: 30 },
          authToken
        ),
      ])
      setReleases(fetchedReleases)
      setBundles(fetchedBundles)

      // Set active release or first release if available
      const active = fetchedReleases.find((r) =>
        ['SHADOW', 'CANARY', 'ACTIVE'].includes(r.state)
      )
      const current = active || fetchedReleases[0] || null
      setSelectedRelease(current)

      if (current) {
        setCanaryPercentage(current.canary_percentage || 10)
        const [fetchedAudits, fetchedMetrics] = await Promise.all([
          releaseControlApi.listAudits(tenantId, current.id, authToken),
          releaseControlApi.getCanaryMetrics(tenantId, current.id, authToken).catch(() => null),
        ])
        setAudits(fetchedAudits)
        setMetrics(fetchedMetrics)
      } else {
        setAudits([])
        setMetrics(null)
      }
    } catch (err) {
      if (err instanceof ReleaseControlApiError) {
        setActionError(`Failed to load releases: ${err.message}`)
      } else {
        setActionError('An unexpected error occurred while loading release control data.')
      }
    } finally {
      setIsLoading(false)
    }
  }, [tenantId, targetComponent, authToken])

  useEffect(() => {
    fetchReleasesAndBundles()
  }, [fetchReleasesAndBundles])

  const selectRelease = async (release: PolicyRelease) => {
    setSelectedRelease(release)
    setCanaryPercentage(release.canary_percentage || 10)
    setActionError(null)
    setActionSuccess(null)
    try {
      const [fetchedAudits, fetchedMetrics] = await Promise.all([
        releaseControlApi.listAudits(tenantId, release.id, authToken),
        releaseControlApi.getCanaryMetrics(tenantId, release.id, authToken).catch(() => null),
      ])
      setAudits(fetchedAudits)
      setMetrics(fetchedMetrics)
    } catch {
      // Ignored non-critical load failure
    }
  }

  // Action handlers
  const handleBootstrapIncumbent = async () => {
    setIsLoading(true)
    setActionError(null)
    try {
      const bundle = await releaseControlApi.bootstrapIncumbent(
        tenantId,
        targetComponent,
        authToken
      )
      setActionSuccess(`Baseline incumbent bundle bootstrapped: ${bundle.bundle_name}`)
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to bootstrap incumbent bundle.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleCreateRelease = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedCandidateBundleId || !selectedIncumbentBundleId) {
      setActionError('Please select both a candidate bundle and an incumbent bundle.')
      return
    }
    setIsLoading(true)
    setActionError(null)
    try {
      const release = await releaseControlApi.createRelease(
        tenantId,
        {
          target_component: targetComponent,
          bundle_id: selectedCandidateBundleId,
          incumbent_bundle_id: selectedIncumbentBundleId,
        },
        authToken
      )
      setActionSuccess(`Draft release ${release.id.slice(0, 8)} created successfully.`)
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to create release.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleSubmitForApproval = async () => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.submitForApproval(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          target_state: 'PENDING_APPROVAL',
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess('Release submitted for human approval.')
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to submit release.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleReviewRelease = async (decision: 'APPROVED' | 'REJECTED') => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.reviewRelease(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          decision,
          decision_comments: reviewComments || undefined,
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess(`Release decision recorded: ${decision}`)
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Review action failed.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleActivateShadow = async () => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.activateShadow(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          target_state: 'SHADOW',
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess('Shadow mode activated. Traffic mirrored asynchronously.')
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to activate shadow mode.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleActivateCanary = async () => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.activateCanary(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          target_state: 'CANARY',
          canary_percentage: canaryPercentage,
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess(`Canary mode activated at ${canaryPercentage}% traffic.`)
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to activate canary mode.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleUpdateCanaryPercentage = async () => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.updateCanaryPercentage(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          target_state: 'CANARY',
          canary_percentage: canaryPercentage,
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess(`Canary percentage updated to ${canaryPercentage}%.`)
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to update canary percentage.')
    } finally {
      setIsLoading(false)
    }
  }

  const handlePromoteActive = async () => {
    if (!selectedRelease) return
    if (!window.confirm('Are you sure you want to promote this candidate to 100% ACTIVE production traffic?')) {
      return
    }
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.promoteActive(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          target_state: 'ACTIVE',
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess('Candidate promoted to 100% ACTIVE production policy.')
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to promote active.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleTriggerRollback = async () => {
    if (!selectedRelease) return
    if (!rollbackReason.trim()) {
      setActionError('A specific rollback reason is required before triggering rollback.')
      return
    }
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.triggerRollback(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          rollback_reason: rollbackReason.trim(),
        },
        authToken
      )
      setSelectedRelease(updated)
      setActionSuccess(`Release rolled back successfully: ${rollbackReason}`)
      setRollbackReason('')
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to execute rollback.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleTripKillSwitch = async () => {
    if (!selectedRelease) return
    if (!killSwitchJustification.trim()) {
      setActionError('Kill-switch justification is mandatory.')
      return
    }
    setIsLoading(true)
    setActionError(null)
    try {
      const updated = await releaseControlApi.tripKillSwitch(
        tenantId,
        selectedRelease.id,
        {
          expected_version: selectedRelease.version,
          justification: killSwitchJustification.trim(),
        },
        authToken
      )
      setSelectedRelease(updated)
      setShowKillSwitchModal(false)
      setKillSwitchJustification('')
      setActionSuccess('EMERGENCY KILL SWITCH TRIPPED: Traffic restored to incumbent and consequential mutations fenced.')
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Failed to trip kill switch.')
    } finally {
      setIsLoading(false)
    }
  }

  const handleRunGuardrailCheck = async () => {
    if (!selectedRelease) return
    setIsLoading(true)
    setActionError(null)
    try {
      const res = await releaseControlApi.runGuardrailCheck(
        tenantId,
        selectedRelease.id,
        0,
        '',
        authToken
      )
      if (res.rolled_back) {
        setActionSuccess(`Guardrail tripped automatic rollback: ${res.reason}`)
      } else {
        setActionSuccess(`Guardrails healthy: ${res.reason}`)
      }
      await fetchReleasesAndBundles()
    } catch (err: any) {
      setActionError(err.message || 'Guardrail check failed.')
    } finally {
      setIsLoading(false)
    }
  }

  const getStateBadgeClass = (state: string) => {
    switch (state) {
      case 'DRAFT':
        return 'badge-draft'
      case 'PENDING_APPROVAL':
        return 'badge-pending'
      case 'APPROVED':
        return 'badge-approved'
      case 'SHADOW':
        return 'badge-shadow'
      case 'CANARY':
        return 'badge-canary'
      case 'ACTIVE':
        return 'badge-active'
      case 'ROLLED_BACK':
        return 'badge-rollback'
      case 'REVOKED':
        return 'badge-revoked'
      default:
        return 'badge-default'
    }
  }

  return (
    <div className="release-control-view">
      <header className="release-control-header">
        <div>
          <h2>Feature E6 — Versioned Policy Canary, Release Control & Rollback</h2>
          <p className="subtitle">
            Controlled deployment of evaluated policy improvements via immutable signed bundles, human sign-offs, and automated safety gates.
          </p>
        </div>

        <div className="header-meta">
          <label>
            Target Component:
            <select
              value={targetComponent}
              onChange={(e) => setTargetComponent(e.target.value)}
              disabled={isLoading}
            >
              <option value="investigator">investigator</option>
              <option value="planner">planner</option>
              <option value="tools">tools</option>
              <option value="recovery">recovery</option>
            </select>
          </label>

          <button
            onClick={handleBootstrapIncumbent}
            disabled={isLoading}
            className="btn-secondary"
            title="Bootstrap v1.0.0 baseline incumbent bundle if missing"
          >
            Bootstrap Incumbent
          </button>

          {authenticatedUser && (
            <div className="auth-badge">
              <span className="auth-user">👤 {authenticatedUser.actor_id}</span>
              <span className="auth-roles">[{authenticatedUser.roles.join(', ')}]</span>
            </div>
          )}
        </div>
      </header>

      {actionError && (
        <div className="alert alert-error" role="alert">
          <strong>Error: </strong> {actionError}
        </div>
      )}

      {actionSuccess && (
        <div className="alert alert-success" role="status">
          <strong>Success: </strong> {actionSuccess}
        </div>
      )}

      {/* Main Grid: Release Cockpit & Metrics */}
      <div className="release-control-grid">
        {/* Left Column: Active / Selected Release Details & Controls */}
        <section className="release-cockpit-card">
          <div className="card-header">
            <h3>Release Lifecycle Cockpit</h3>
            {selectedRelease && (
              <span className={`status-badge ${getStateBadgeClass(selectedRelease.state)}`}>
                {selectedRelease.state}
              </span>
            )}
          </div>

          {selectedRelease ? (
            <div className="cockpit-body">
              <div className="details-grid">
                <div>
                  <label>Release ID:</label>
                  <code>{selectedRelease.id}</code>
                </div>
                <div>
                  <label>Version (CAS):</label>
                  <span>v{selectedRelease.version}</span>
                </div>
                <div>
                  <label>Component:</label>
                  <span>{selectedRelease.target_component}</span>
                </div>
                <div>
                  <label>Canary Traffic:</label>
                  <strong>{selectedRelease.canary_percentage}%</strong>
                </div>
                <div>
                  <label>Candidate Bundle:</label>
                  <code>{selectedRelease.bundle_id.slice(0, 12)}...</code>
                </div>
                <div>
                  <label>Incumbent Bundle:</label>
                  <code>{selectedRelease.incumbent_bundle_id.slice(0, 12)}...</code>
                </div>
                <div>
                  <label>Drafter:</label>
                  <span>{selectedRelease.drafter_id}</span>
                </div>
                <div>
                  <label>Reviewer:</label>
                  <span>{selectedRelease.reviewer_id ? `${selectedRelease.reviewer_id} (${selectedRelease.reviewer_role})` : 'None'}</span>
                </div>
              </div>

              {selectedRelease.kill_switch_active && (
                <div className="kill-switch-banner">
                  🚨 <strong>KILL SWITCH ACTIVATED</strong> by {selectedRelease.kill_switch_triggered_by} at {selectedRelease.kill_switch_triggered_at}. Consequential mutations are fenced.
                </div>
              )}

              {selectedRelease.rollback_reason && (
                <div className="rollback-banner">
                  ⚠️ <strong>Rolled back:</strong> {selectedRelease.rollback_reason}
                </div>
              )}

              {/* State Transition Actions */}
              <div className="cockpit-actions">
                <h4>Lifecycle Actions</h4>

                {selectedRelease.state === 'DRAFT' && (
                  <button
                    onClick={handleSubmitForApproval}
                    disabled={isLoading}
                    className="btn-primary"
                  >
                    Submit for Human Review
                  </button>
                )}

                {selectedRelease.state === 'PENDING_APPROVAL' && (
                  <div className="review-box">
                    <input
                      type="text"
                      placeholder="Optional review comments..."
                      value={reviewComments}
                      onChange={(e) => setReviewComments(e.target.value)}
                      disabled={isLoading}
                    />
                    <div className="button-group">
                      <button
                        onClick={() => handleReviewRelease('APPROVED')}
                        disabled={isLoading}
                        className="btn-success"
                      >
                        Approve Release
                      </button>
                      <button
                        onClick={() => handleReviewRelease('REJECTED')}
                        disabled={isLoading}
                        className="btn-danger"
                      >
                        Reject Release
                      </button>
                    </div>
                  </div>
                )}

                {selectedRelease.state === 'APPROVED' && (
                  <div className="button-group">
                    <button
                      onClick={handleActivateShadow}
                      disabled={isLoading}
                      className="btn-secondary"
                    >
                      Activate Shadow Mode (0%)
                    </button>
                    <button
                      onClick={handleActivateCanary}
                      disabled={isLoading}
                      className="btn-primary"
                    >
                      Activate Canary Mode ({canaryPercentage}%)
                    </button>
                  </div>
                )}

                {selectedRelease.state === 'SHADOW' && (
                  <div className="button-group">
                    <button
                      onClick={handleActivateCanary}
                      disabled={isLoading}
                      className="btn-primary"
                    >
                      Promote from Shadow to Canary
                    </button>
                    <button
                      onClick={handleTriggerRollback}
                      disabled={isLoading}
                      className="btn-danger"
                    >
                      Terminate Shadow Mode
                    </button>
                  </div>
                )}

                {selectedRelease.state === 'CANARY' && (
                  <div className="canary-controls">
                    <div className="slider-box">
                      <label>Canary Traffic Allocation: {canaryPercentage}%</label>
                      <input
                        type="range"
                        min="1"
                        max="100"
                        value={canaryPercentage}
                        onChange={(e) => setCanaryPercentage(Number(e.target.value))}
                        disabled={isLoading}
                      />
                      <button
                        onClick={handleUpdateCanaryPercentage}
                        disabled={isLoading}
                        className="btn-secondary"
                      >
                        Update Allocation
                      </button>
                    </div>

                    <div className="button-group">
                      <button
                        onClick={handlePromoteActive}
                        disabled={isLoading}
                        className="btn-success"
                      >
                        Promote to 100% Active Production
                      </button>
                    </div>
                  </div>
                )}

                {/* Rollback & Kill Switch (Active across SHADOW, CANARY, ACTIVE) */}
                {['SHADOW', 'CANARY', 'ACTIVE'].includes(selectedRelease.state) && (
                  <div className="rollback-controls">
                    <div className="rollback-input-group">
                      <input
                        type="text"
                        placeholder="Rollback reason (required)..."
                        value={rollbackReason}
                        onChange={(e) => setRollbackReason(e.target.value)}
                        disabled={isLoading}
                      />
                      <button
                        onClick={handleTriggerRollback}
                        disabled={isLoading || !rollbackReason.trim()}
                        className="btn-warning"
                      >
                        Trigger Rollback
                      </button>
                    </div>

                    <button
                      onClick={() => setShowKillSwitchModal(true)}
                      disabled={isLoading}
                      className="btn-kill-switch"
                    >
                      🚨 EMERGENCY KILL SWITCH
                    </button>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <p className="empty-message">No release found for component '{targetComponent}'. Create a draft release below.</p>
          )}
        </section>

        {/* Right Column: Canary Health & Guardrail Metrics */}
        <section className="guardrail-metrics-card">
          <div className="card-header">
            <h3>Canary Safety Guardrails</h3>
            <button
              onClick={handleRunGuardrailCheck}
              disabled={isLoading || !selectedRelease}
              className="btn-small"
            >
              Evaluate Now
            </button>
          </div>

          {metrics ? (
            <div className="metrics-body">
              <div className="metrics-summary">
                <div className="metric-tile">
                  <span className="metric-label">Recommendation:</span>
                  <span className={`metric-badge ${metrics.recommendation === 'HEALTHY' ? 'healthy' : 'warning'}`}>
                    {metrics.recommendation}
                  </span>
                </div>
                <div className="metric-tile">
                  <span className="metric-label">Invariant Breaches (N=1):</span>
                  <span className={metrics.invariant_breach_count > 0 ? 'breach' : 'ok'}>
                    {metrics.invariant_breach_count}
                  </span>
                </div>
              </div>

              <div className="table-responsive">
                <table className="metrics-table">
                  <thead>
                    <tr>
                      <th>Cohort</th>
                      <th>Sample (N)</th>
                      <th>Error Rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>Incumbent Baseline</td>
                      <td>{metrics.incumbent_sample_size}</td>
                      <td>{(metrics.incumbent_error_rate * 100).toFixed(1)}%</td>
                    </tr>
                    <tr>
                      <td>Candidate Arm</td>
                      <td>{metrics.candidate_sample_size}</td>
                      <td>{(metrics.candidate_error_rate * 100).toFixed(1)}%</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <div className="rr-stat">
                <span>Relative Risk (RR vs Incumbent): </span>
                <strong>{metrics.relative_risk !== null ? metrics.relative_risk.toFixed(2) : 'N/A'}</strong>
                <span className="threshold-hint">(Threshold: RR &gt; 1.5 triggers rollback)</span>
              </div>
            </div>
          ) : (
            <p className="empty-message">No active canary metrics available for this release.</p>
          )}
        </section>
      </div>

      {/* Draft New Release Form */}
      <section className="create-release-section">
        <h3>Draft New Policy Release</h3>
        <form onSubmit={handleCreateRelease} className="create-release-form">
          <div className="form-group">
            <label>Sealed Candidate Bundle:</label>
            <select
              value={selectedCandidateBundleId}
              onChange={(e) => setSelectedCandidateBundleId(e.target.value)}
              disabled={isLoading}
              required
            >
              <option value="">-- Select Candidate Bundle --</option>
              {bundles
                .filter((b) => b.status === 'SEALED')
                .map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.bundle_name} ({b.bundle_sha256.slice(0, 8)}...)
                  </option>
                ))}
            </select>
          </div>

          <div className="form-group">
            <label>Incumbent Baseline Bundle:</label>
            <select
              value={selectedIncumbentBundleId}
              onChange={(e) => setSelectedIncumbentBundleId(e.target.value)}
              disabled={isLoading}
              required
            >
              <option value="">-- Select Incumbent Bundle --</option>
              {bundles
                .filter((b) => b.status === 'SEALED')
                .map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.bundle_name} ({b.bundle_sha256.slice(0, 8)}...)
                  </option>
                ))}
            </select>
          </div>

          <button type="submit" disabled={isLoading} className="btn-primary">
            Create Draft Release
          </button>
        </form>
      </section>

      {/* Release History & Audits */}
      <div className="tables-grid">
        <section className="history-section">
          <h3>Releases for {targetComponent}</h3>
          <div className="table-responsive">
            <table className="data-table">
              <thead>
                <tr>
                  <th>State</th>
                  <th>Version</th>
                  <th>Traffic</th>
                  <th>Drafter</th>
                  <th>Reviewer</th>
                  <th>Created</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {releases.map((rel) => (
                  <tr
                    key={rel.id}
                    className={selectedRelease?.id === rel.id ? 'row-selected' : ''}
                  >
                    <td>
                      <span className={`status-badge ${getStateBadgeClass(rel.state)}`}>
                        {rel.state}
                      </span>
                    </td>
                    <td>v{rel.version}</td>
                    <td>{rel.canary_percentage}%</td>
                    <td>{rel.drafter_id}</td>
                    <td>{rel.reviewer_id || '—'}</td>
                    <td>{new Date(rel.created_at).toLocaleTimeString()}</td>
                    <td>
                      <button
                        onClick={() => selectRelease(rel)}
                        className="btn-link"
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="audits-section">
          <h3>Immutable Audit Trail (Release {selectedRelease?.id.slice(0, 8)})</h3>
          <div className="table-responsive">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Transition</th>
                  <th>Actor</th>
                  <th>Role</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {audits.map((aud) => (
                  <tr key={aud.id}>
                    <td><code>{aud.event_type}</code></td>
                    <td>
                      {aud.from_state ? `${aud.from_state} → ` : ''}
                      <strong>{aud.to_state}</strong>
                    </td>
                    <td>{aud.actor_id}</td>
                    <td>{aud.actor_role}</td>
                    <td>{new Date(aud.created_at).toLocaleTimeString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {/* Kill Switch Modal */}
      {showKillSwitchModal && (
        <div className="modal-backdrop">
          <div className="kill-switch-modal">
            <h3>🚨 Confirm Emergency Kill Switch</h3>
            <p>
              Tripping the kill switch will <strong>immediately terminate all candidate traffic</strong> and revert 100% of incoming incident admissions to the baseline incumbent policy. Consequential mutations on in-flight candidate runs will be server-fenced.
            </p>
            <div className="form-group">
              <label>Mandatory Justification / Incident Root Cause:</label>
              <textarea
                rows={3}
                placeholder="Explain why the kill switch is being tripped..."
                value={killSwitchJustification}
                onChange={(e) => setKillSwitchJustification(e.target.value)}
              />
            </div>
            <div className="modal-actions">
              <button
                onClick={() => setShowKillSwitchModal(false)}
                className="btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleTripKillSwitch}
                disabled={!killSwitchJustification.trim()}
                className="btn-kill-switch"
              >
                Trip Kill Switch Now
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
