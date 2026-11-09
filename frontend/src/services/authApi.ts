/**
 * Authentication client for OpsBlaze.
 * Development identity names are labels only; credentials are entered by the operator.
 * Access tokens remain in React memory and are not written to browser storage.
 */

export type ActorKind = 'HUMAN' | 'SYSTEM' | 'MODEL' | 'BOT'

export interface AuthenticatedUser {
  actor_id: string
  actor_kind: ActorKind
  roles: string[]
  tenant_id: string
  auth_mechanism?: string
  can_review_evaluation?: boolean
  can_approve_release?: boolean
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  principal: AuthenticatedUser
}

export interface PreconfiguredUserPreset {
  username: string
  label: string
}

export const PRECONFIGURED_PRESETS: PreconfiguredUserPreset[] = [
  { username: 'incident_commander', label: 'Incident Commander (development)' },
  { username: 'release_owner', label: 'Release Owner (development)' },
  { username: 'platform_engineer', label: 'Platform Engineer (development)' },
  { username: 'evaluation_owner', label: 'Evaluation Owner (development)' },
  { username: 'sre_lead', label: 'SRE Lead (development)' },
  { username: 'operator', label: 'Operator (development)' },
  { username: 'bot_agent', label: 'Bot Agent (development)' },
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parsePrincipal(value: unknown): AuthenticatedUser {
  if (!isRecord(value)) throw new Error('Authentication response has no principal object.')
  const { actor_id, actor_kind, roles, tenant_id } = value
  const actorKinds: ActorKind[] = ['HUMAN', 'SYSTEM', 'MODEL', 'BOT']
  if (
    typeof actor_id !== 'string' ||
    typeof tenant_id !== 'string' ||
    typeof actor_kind !== 'string' ||
    !actorKinds.includes(actor_kind as ActorKind) ||
    !Array.isArray(roles) ||
    !roles.every((role) => typeof role === 'string')
  ) {
    throw new Error('Authentication response contains an invalid principal.')
  }
  return {
    actor_id,
    actor_kind: actor_kind as ActorKind,
    tenant_id,
    roles,
    auth_mechanism: typeof value.auth_mechanism === 'string' ? value.auth_mechanism : undefined,
    can_review_evaluation:
      typeof value.can_review_evaluation === 'boolean' ? value.can_review_evaluation : undefined,
    can_approve_release:
      typeof value.can_approve_release === 'boolean' ? value.can_approve_release : undefined,
  }
}

async function readError(response: Response): Promise<Error> {
  const body = await response.json().catch(() => ({ detail: response.statusText }))
  const detail = isRecord(body) ? body.detail : undefined
  return new Error(typeof detail === 'string' ? detail : `Request failed: HTTP ${response.status}`)
}

export const authApi = {
  async login(username: string, password: string, tenantId: string): Promise<LoginResponse> {
    const response = await fetch('/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, tenant_id: tenantId }),
    })
    if (!response.ok) throw await readError(response)

    const body: unknown = await response.json()
    if (
      !isRecord(body) ||
      typeof body.access_token !== 'string' ||
      typeof body.token_type !== 'string' ||
      typeof body.expires_in !== 'number'
    ) {
      throw new Error('Authentication endpoint returned an invalid token response.')
    }
    return {
      access_token: body.access_token,
      token_type: body.token_type,
      expires_in: body.expires_in,
      principal: parsePrincipal(body.principal),
    }
  },

  async me(token: string): Promise<AuthenticatedUser> {
    const response = await fetch('/v1/auth/me', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok) throw await readError(response)
    return parsePrincipal(await response.json())
  },

  async revoke(token: string): Promise<void> {
    const response = await fetch('/v1/auth/revoke', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({}),
    })
    if (!response.ok) throw await readError(response)
  },
}
