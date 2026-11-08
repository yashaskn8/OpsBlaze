/**
 * Authentication service for OpsBlaze.
 * Communicates with /v1/auth/login and /v1/auth/revoke.
 */

export interface AuthenticatedUser {
  actor_id: string
  actor_kind: 'human' | 'bot' | 'system'
  roles: string[]
  tenant_id: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  actor_id: string
  actor_kind: string
  roles: string[]
  tenant_id: string
}

export interface PreconfiguredUserPreset {
  username: string
  label: string
  defaultRole: string
  password: string
  description: string
}

export const PRECONFIGURED_PRESETS: PreconfiguredUserPreset[] = [
  {
    username: 'incident_commander',
    label: 'Incident Commander',
    defaultRole: 'incident_commander',
    password: 'incident-commander-secret-pass-2026',
    description: 'Human responder authorized for incident approvals and canary activations',
  },
  {
    username: 'release_owner',
    label: 'Release Owner',
    defaultRole: 'release_owner',
    password: 'release-owner-secret-pass-2026',
    description: 'Release governance owner authorized for E6 reviews, rollouts, and promotions',
  },
  {
    username: 'platform_engineer',
    label: 'Platform Engineer',
    defaultRole: 'platform_engineer',
    password: 'platform-engineer-secret-pass-2026',
    description: 'Infrastructure engineer authorized to create bundles, draft releases, and activate shadow',
  },
  {
    username: 'evaluation_owner',
    label: 'Evaluation Owner',
    defaultRole: 'evaluation_owner',
    password: 'evaluation-owner-secret-pass-2026',
    description: 'E5 evaluation owner authorized for candidate testing and regression gating',
  },
  {
    username: 'sre_lead',
    label: 'SRE Lead',
    defaultRole: 'sre_lead',
    password: 'sre-lead-secret-pass-2026',
    description: 'Reliability lead authorized for rollback and emergency kill-switch',
  },
  {
    username: 'operator',
    label: 'Standard Operator',
    defaultRole: 'operator',
    password: 'operator-secret-pass-2026',
    description: 'Standard responder with observation and emergency mitigation rights',
  },
  {
    username: 'bot_agent',
    label: 'Bot Agent (Non-human)',
    defaultRole: 'operator',
    password: 'bot-agent-secret-pass-2026',
    description: 'Automated agent (strictly forbidden from human approval and release promotion)',
  },
]

export const authApi = {
  async login(username: string, password: string, tenantId: string): Promise<LoginResponse> {
    const res = await fetch('/v1/auth/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        username,
        password,
        tenant_id: tenantId,
      }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(typeof err.detail === 'string' ? err.detail : `Login failed: HTTP ${res.status}`)
    }
    return (await res.json()) as LoginResponse
  },

  async revoke(token: string): Promise<void> {
    await fetch('/v1/auth/revoke', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
      },
    })
  },
}
