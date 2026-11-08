import { useState, useEffect } from 'react'
import type { ApprovalResponse } from '../types/stage6'

interface Props {
  incidentId: string
  tenantId: string
  activeApproval: ApprovalResponse | null
  isLockedOut: boolean
  onRefresh: () => void
  authToken?: string
  authenticatedUser?: {
    actor_id: string
    actor_kind: string
    roles: string[]
  } | null
}

export function ApprovalCockpitView({
  incidentId,
  tenantId,
  activeApproval,
  isLockedOut,
  onRefresh,
  authToken,
  authenticatedUser,
}: Props) {
  const [reviewerId, setReviewerId] = useState(authenticatedUser?.actor_id || 'commander-alice')
  const [reviewerRole, setReviewerRole] = useState(authenticatedUser?.roles[0] || 'incident_commander')
  const [reason, setReason] = useState('Reviewed diagnostic telemetry and verified safety')
  const [actionLoading, setActionLoading] = useState(false)
  const [statusMessage, setStatusMessage] = useState<string | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [timeLeftSec, setTimeLeftSec] = useState<number>(0)

  useEffect(() => {
    if (authenticatedUser) {
      setReviewerId(authenticatedUser.actor_id)
      if (authenticatedUser.roles.length > 0) {
        setReviewerRole(authenticatedUser.roles[0])
      }
    }
  }, [authenticatedUser])

  const isHuman = authenticatedUser ? authenticatedUser.actor_kind === 'human' : true
  const canApprove =
    isHuman &&
    (!authenticatedUser ||
      authenticatedUser.roles.some((r) =>
        ['incident_commander', 'operator', 'platform_engineer', 'sre_lead'].includes(r.toLowerCase())
      ))

  // Countdown timer for expiration
  useEffect(() => {
    if (!activeApproval || !activeApproval.expires_at) {
      setTimeLeftSec(0)
      return
    }
    const updateTime = () => {
      const diff = Math.max(
        0,
        Math.floor((new Date(activeApproval.expires_at).getTime() - Date.now()) / 1000)
      )
      setTimeLeftSec(diff)
    }
    updateTime()
    const interval = setInterval(updateTime, 1000)
    return () => clearInterval(interval)
  }, [activeApproval])

  const handleDecision = async (decision: 'approve' | 'reject' | 'revoke') => {
    if (!activeApproval) return
    setActionLoading(true)
    setStatusMessage(null)
    setErrorMessage(null)

    try {
      const endpoint = `/v1/approvals/${activeApproval.id}/${decision}`
      const body = {
        reviewer_id: reviewerId.trim(),
        reviewer_role: reviewerRole.trim(),
        reason: reason.trim(),
      }

      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
      }
      if (authToken) {
        headers['Authorization'] = `Bearer ${authToken}`
      }

      const res = await fetch(endpoint, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      })

      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || `Decision failed with HTTP ${res.status}`)
      }

      const updated = (await res.json()) as ApprovalResponse
      setStatusMessage(`Successfully executed ${decision.toUpperCase()} on approval ${updated.id}. State: ${updated.state}`)
      onRefresh()
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setActionLoading(false)
    }
  }

  if (!activeApproval) {
    return (
      <div className="b1-panel" style={{ padding: '20px', background: '#f8fafc', borderRadius: '8px' }}>
        <h3>P2 — Version-Bound Approval Cockpit</h3>
        <p style={{ color: '#64748b' }}>
          No active approval request found for Incident <code>{incidentId}</code>. When the adaptive investigator proposes a repair plan, the approval request will appear here with version fences.
        </p>
      </div>
    )
  }

  const isPending = activeApproval.state === 'PENDING'
  const isApproved = activeApproval.state === 'APPROVED'

  return (
    <div className="b1-panel" style={{ padding: '20px', background: '#f8fafc', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3>P2 — Version-Bound Approval Cockpit</h3>
        <span
          style={{
            padding: '4px 10px',
            borderRadius: '12px',
            fontWeight: 'bold',
            fontSize: '12px',
            backgroundColor: isPending ? '#fef3c7' : isApproved ? '#dcfce7' : '#fee2e2',
            color: isPending ? '#92400e' : isApproved ? '#166534' : '#991b1b',
          }}
        >
          {activeApproval.state}
        </span>
      </div>

      <dl style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', margin: '16px 0' }}>
        <div>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Plan ID</dt>
          <dd style={{ margin: 0, fontWeight: 600 }}><code>{activeApproval.plan_id}</code></dd>
        </div>
        <div>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Target ID</dt>
          <dd style={{ margin: 0, fontWeight: 600 }}><code>{activeApproval.target_id}</code></dd>
        </div>
        <div>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Expected Target Version</dt>
          <dd style={{ margin: 0, fontWeight: 600 }}><code>{activeApproval.expected_resource_version}</code></dd>
        </div>
        <div>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Time Remaining (TTL)</dt>
          <dd style={{ margin: 0, fontWeight: 600, color: timeLeftSec < 60 ? '#dc2626' : '#1e293b' }}>
            {timeLeftSec}s
          </dd>
        </div>
        <div style={{ gridColumn: 'span 2' }}>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Canonical Plan SHA-256 Digest</dt>
          <dd style={{ margin: 0 }}><code style={{ fontSize: '11px', wordBreak: 'break-all' }}>{activeApproval.canonical_plan_digest}</code></dd>
        </div>
        <div style={{ gridColumn: 'span 2' }}>
          <dt style={{ fontSize: '11px', color: '#64748b' }}>Action Digest</dt>
          <dd style={{ margin: 0 }}><code style={{ fontSize: '11px', wordBreak: 'break-all' }}>{activeApproval.action_digest}</code></dd>
        </div>
      </dl>

      {statusMessage && (
        <div style={{ padding: '8px 12px', background: '#dcfce7', color: '#166534', borderRadius: '4px', marginBottom: '12px' }}>
          {statusMessage}
        </div>
      )}
      {errorMessage && (
        <div style={{ padding: '8px 12px', background: '#fee2e2', color: '#991b1b', borderRadius: '4px', marginBottom: '12px' }}>
          {errorMessage}
        </div>
      )}

      <div style={{ borderTop: '1px solid #e2e8f0', paddingTop: '16px' }}>
        <h4 style={{ margin: '0 0 12px 0' }}>Operator Verification & Decision Dispatch</h4>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#475569', marginBottom: '4px' }}>Reviewer Identity</label>
            <input
              type="text"
              value={reviewerId}
              onChange={(e) => setReviewerId(e.target.value)}
              disabled={isLockedOut || actionLoading}
              style={{ width: '100%', padding: '6px 8px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '12px', color: '#475569', marginBottom: '4px' }}>Authorized Human Role</label>
            <select
              value={reviewerRole}
              onChange={(e) => setReviewerRole(e.target.value)}
              disabled={isLockedOut || actionLoading}
              style={{ width: '100%', padding: '6px 8px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
            >
              <option value="incident_commander">Incident Commander</option>
              <option value="platform_engineer">Platform Engineer</option>
              <option value="sre_lead">SRE Lead</option>
            </select>
          </div>
        </div>

        <div style={{ marginBottom: '16px' }}>
          <label style={{ display: 'block', fontSize: '12px', color: '#475569', marginBottom: '4px' }}>Verification Reason / Justification</label>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={isLockedOut || actionLoading}
            style={{ width: '100%', padding: '6px 8px', borderRadius: '4px', border: '1px solid #cbd5e1' }}
          />
        </div>

        {!canApprove && (
          <div style={{ marginBottom: '12px', padding: '8px 12px', backgroundColor: '#fef2f2', border: '1px solid #f87171', borderRadius: '4px', color: '#991b1b', fontSize: '13px' }}>
            ⚠️ Principal {authenticatedUser ? `'${authenticatedUser.actor_id}' (${authenticatedUser.actor_kind})` : ''} lacks human approval authority. Approval actions are disabled.
          </div>
        )}

        <div style={{ display: 'flex', gap: '10px' }}>
          <button
            type="button"
            onClick={() => void handleDecision('approve')}
            disabled={!isPending || isLockedOut || actionLoading || !canApprove}
            style={{
              padding: '8px 16px',
              backgroundColor: isPending && !isLockedOut && canApprove ? '#16a34a' : '#94a3b8',
              color: '#ffffff',
              border: 'none',
              borderRadius: '4px',
              cursor: isPending && !isLockedOut && canApprove ? 'pointer' : 'not-allowed',
              fontWeight: 600,
            }}
          >
            {actionLoading ? 'Processing...' : 'Approve Plan (v1)'}
          </button>

          <button
            type="button"
            onClick={() => void handleDecision('reject')}
            disabled={!isPending || isLockedOut || actionLoading || !canApprove}
            style={{
              padding: '8px 16px',
              backgroundColor: isPending && !isLockedOut && canApprove ? '#dc2626' : '#94a3b8',
              color: '#ffffff',
              border: 'none',
              borderRadius: '4px',
              cursor: isPending && !isLockedOut && canApprove ? 'pointer' : 'not-allowed',
              fontWeight: 600,
            }}
          >
            Reject Plan
          </button>

          <button
            type="button"
            onClick={() => void handleDecision('revoke')}
            disabled={!isApproved || isLockedOut || actionLoading || !canApprove}
            style={{
              padding: '8px 16px',
              backgroundColor: isApproved && !isLockedOut && canApprove ? '#d97706' : '#94a3b8',
              color: '#ffffff',
              border: 'none',
              borderRadius: '4px',
              cursor: isApproved && !isLockedOut && canApprove ? 'pointer' : 'not-allowed',
              fontWeight: 600,
            }}
          >
            Revoke Approval
          </button>
        </div>
      </div>
    </div>
  )
}
