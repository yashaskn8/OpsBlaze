/**
 * Typed frontend API service for Feature E6 — Policy Release Control & Automated Rollback.
 * Enforces typed request contracts, explicit version CAS tokens, and structured error handling.
 */

import type {
  CanaryMetrics,
  KillSwitchRequest,
  PolicyBundle,
  PolicyRelease,
  PolicyReleaseCreateRequest,
  ReleaseAuditEvent,
  ReleaseReviewRequest,
  ReleaseTransitionRequest,
  RollbackRequest,
} from '../types/stage11'

export interface ApiErrorDetail {
  status: number
  message: string
  raw?: unknown
}

export class ReleaseControlApiError extends Error {
  status: number
  raw?: unknown

  constructor(detail: ApiErrorDetail) {
    super(detail.message)
    this.name = 'ReleaseControlApiError'
    this.status = detail.status
    this.raw = detail.raw
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let errBody: any = {}
    try {
      errBody = await res.json()
    } catch {
      errBody = { detail: await res.text() }
    }

    let message = `HTTP ${res.status}: ${res.statusText}`
    if (errBody && typeof errBody === 'object') {
      if (errBody.detail) {
        if (typeof errBody.detail === 'string') {
          message = errBody.detail
        } else if (Array.isArray(errBody.detail)) {
          message = errBody.detail.map((e: any) => `${e.loc?.join('.') || 'field'}: ${e.msg}`).join(', ')
        } else {
          message = JSON.stringify(errBody.detail)
        }
      }
    }

    throw new ReleaseControlApiError({
      status: res.status,
      message,
      raw: errBody,
    })
  }

  return (await res.json()) as T
}

function getHeaders(tenantId: string, token?: string): HeadersInit {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Tenant-ID': tenantId,
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

export const releaseControlApi = {
  // Bundles
  async listBundles(
    tenantId: string,
    params?: { target_component?: string; status?: string; limit?: number },
    token?: string
  ): Promise<PolicyBundle[]> {
    const query = new URLSearchParams()
    if (params?.target_component) query.set('target_component', params.target_component)
    if (params?.status) query.set('status', params.status)
    if (params?.limit) query.set('limit', String(params.limit))

    const url = `/v1/bundles${query.toString() ? `?${query.toString()}` : ''}`
    const res = await fetch(url, {
      method: 'GET',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<PolicyBundle[]>(res)
  },

  async sealBundle(tenantId: string, bundleId: string, token?: string): Promise<PolicyBundle> {
    const res = await fetch(`/v1/bundles/${bundleId}/seal`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<PolicyBundle>(res)
  },

  async bootstrapIncumbent(
    tenantId: string,
    targetComponent: string,
    token?: string
  ): Promise<PolicyBundle> {
    const query = new URLSearchParams({ target_component: targetComponent })
    const res = await fetch(`/v1/bundles/bootstrap?${query.toString()}`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<PolicyBundle>(res)
  },

  // Releases
  async listReleases(
    tenantId: string,
    params?: { target_component?: string; state?: string; limit?: number },
    token?: string
  ): Promise<PolicyRelease[]> {
    const query = new URLSearchParams()
    if (params?.target_component) query.set('target_component', params.target_component)
    if (params?.state) query.set('state', params.state)
    if (params?.limit) query.set('limit', String(params.limit))

    const url = `/v1/releases${query.toString() ? `?${query.toString()}` : ''}`
    const res = await fetch(url, {
      method: 'GET',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<PolicyRelease[]>(res)
  },

  async getRelease(tenantId: string, releaseId: string, token?: string): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}`, {
      method: 'GET',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async createRelease(
    tenantId: string,
    payload: PolicyReleaseCreateRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch('/v1/releases', {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async submitForApproval(
    tenantId: string,
    releaseId: string,
    payload: ReleaseTransitionRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/submit`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async reviewRelease(
    tenantId: string,
    releaseId: string,
    payload: ReleaseReviewRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/review`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async activateShadow(
    tenantId: string,
    releaseId: string,
    payload: ReleaseTransitionRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/activate-shadow`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async activateCanary(
    tenantId: string,
    releaseId: string,
    payload: ReleaseTransitionRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/activate-canary`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async updateCanaryPercentage(
    tenantId: string,
    releaseId: string,
    payload: ReleaseTransitionRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/update-canary`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async promoteActive(
    tenantId: string,
    releaseId: string,
    payload: ReleaseTransitionRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/promote-active`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async triggerRollback(
    tenantId: string,
    releaseId: string,
    payload: RollbackRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/rollback`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async tripKillSwitch(
    tenantId: string,
    releaseId: string,
    payload: KillSwitchRequest,
    token?: string
  ): Promise<PolicyRelease> {
    const res = await fetch(`/v1/releases/${releaseId}/kill-switch`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify(payload),
    })
    return handleResponse<PolicyRelease>(res)
  },

  async listAudits(
    tenantId: string,
    releaseId: string,
    token?: string
  ): Promise<ReleaseAuditEvent[]> {
    const res = await fetch(`/v1/releases/${releaseId}/audits`, {
      method: 'GET',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<ReleaseAuditEvent[]>(res)
  },

  async getCanaryMetrics(
    tenantId: string,
    releaseId: string,
    token?: string
  ): Promise<CanaryMetrics> {
    const res = await fetch(`/v1/releases/${releaseId}/metrics`, {
      method: 'GET',
      headers: getHeaders(tenantId, token),
    })
    return handleResponse<CanaryMetrics>(res)
  },

  async runGuardrailCheck(
    tenantId: string,
    releaseId: string,
    simulatedBreaches: number = 0,
    detail: string = '',
    token?: string
  ): Promise<{ release_id: string; rolled_back: boolean; reason: string }> {
    const res = await fetch(`/v1/releases/${releaseId}/guardrail-check`, {
      method: 'POST',
      headers: getHeaders(tenantId, token),
      body: JSON.stringify({
        simulated_invariant_breaches: simulatedBreaches,
        invariant_breach_detail: detail,
      }),
    })
    return handleResponse<{ release_id: string; rolled_back: boolean; reason: string }>(res)
  },
}
