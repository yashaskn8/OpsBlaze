/**
 * Typed frontend API service for Feature E5 — Candidate-to-Incumbent Regression Gate.
 * Enforces typed request contracts, explicit version CAS tokens, and structured error handling.
 */

import type {
  CandidateManifestResponse,
  ManifestDetailResponse,
  CandidateTrialResponse,
  CandidateReportResponse,
  CandidateEvaluationCreateRequest,
  ReviewDecision,
} from '../types/stage9'

export interface ApiErrorDetail {
  status: number
  message: string
  rejectionCode?: string
  raw?: unknown
}

export class EvaluationsApiError extends Error {
  status: number
  rejectionCode?: string
  raw?: unknown

  constructor(detail: ApiErrorDetail) {
    super(detail.message)
    this.name = 'EvaluationsApiError'
    this.status = detail.status
    this.rejectionCode = detail.rejectionCode
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
    let rejectionCode: string | undefined

    if (errBody && typeof errBody === 'object') {
      if (errBody.detail) {
        if (typeof errBody.detail === 'string') {
          message = errBody.detail
        } else if (errBody.detail.message) {
          message = errBody.detail.message
          rejectionCode = errBody.detail.rejection_code
        } else if (Array.isArray(errBody.detail)) {
          message = errBody.detail.map((e: any) => `${e.loc?.join('.') || 'field'}: ${e.msg}`).join(', ')
        } else {
          message = JSON.stringify(errBody.detail)
        }
      }
    }

    throw new EvaluationsApiError({
      status: res.status,
      message,
      rejectionCode,
      raw: errBody,
    })
  }

  return (await res.json()) as T
}

function getAuthHeaders(tenantId: string, authToken?: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'X-Tenant-ID': tenantId,
  }
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`
  }
  return headers
}

export const evaluationsApi = {
  async listManifests(tenantId: string, authToken?: string): Promise<CandidateManifestResponse[]> {
    const res = await fetch('/v1/evaluations?limit=50', {
      headers: getAuthHeaders(tenantId, authToken),
    })
    return handleResponse<CandidateManifestResponse[]>(res)
  },

  async getManifestDetail(id: string, tenantId: string, authToken?: string): Promise<ManifestDetailResponse> {
    const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}`, {
      headers: getAuthHeaders(tenantId, authToken),
    })
    return handleResponse<ManifestDetailResponse>(res)
  },

  async createManifest(
    req: CandidateEvaluationCreateRequest,
    tenantId: string,
    authToken?: string
  ): Promise<CandidateManifestResponse> {
    const res = await fetch('/v1/evaluations', {
      method: 'POST',
      headers: getAuthHeaders(tenantId, authToken),
      body: JSON.stringify(req),
    })
    return handleResponse<CandidateManifestResponse>(res)
  },

  async freezeManifest(
    id: string,
    expectedVersion: number,
    tenantId: string,
    authToken?: string
  ): Promise<CandidateManifestResponse> {
    const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/freeze`, {
      method: 'POST',
      headers: getAuthHeaders(tenantId, authToken),
      body: JSON.stringify({ expected_version: expectedVersion }),
    })
    return handleResponse<CandidateManifestResponse>(res)
  },

  async runEvaluation(
    id: string,
    workerId: string,
    leaseSeconds: number,
    tenantId: string,
    authToken?: string
  ): Promise<CandidateReportResponse> {
    const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/run`, {
      method: 'POST',
      headers: getAuthHeaders(tenantId, authToken),
      body: JSON.stringify({ worker_id: workerId, lease_seconds: leaseSeconds }),
    })
    return handleResponse<CandidateReportResponse>(res)
  },

  async getTrials(id: string, tenantId: string, authToken?: string): Promise<CandidateTrialResponse[]> {
    const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/trials`, {
      headers: getAuthHeaders(tenantId, authToken),
    })
    return handleResponse<CandidateTrialResponse[]>(res)
  },

  async submitReview(
    id: string,
    expectedVersion: number,
    decision: ReviewDecision,
    comments: string,
    tenantId: string,
    authToken?: string
  ): Promise<CandidateReportResponse> {
    const res = await fetch(`/v1/evaluations/${encodeURIComponent(id)}/review`, {
      method: 'POST',
      headers: getAuthHeaders(tenantId, authToken),
      body: JSON.stringify({
        decision,
        comments,
        expected_version: expectedVersion,
      }),
    })
    return handleResponse<CandidateReportResponse>(res)
  },

  async getAuthenticatedPrincipal(tenantId: string, authToken?: string): Promise<any> {
    if (!authToken) return null
    try {
      const res = await fetch('/v1/auth/me', {
        headers: getAuthHeaders(tenantId, authToken),
      })
      if (!res.ok) return null
      return await res.json()
    } catch {
      return null
    }
  },
}
