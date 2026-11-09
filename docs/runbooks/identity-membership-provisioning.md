# Runbook: Enterprise Identity Membership Provisioning & 0-Second Revocation

## 1. Overview & Operational Principles

Stage 11.2 establishes an RFC 7662 OAuth 2.0 token introspection gateway (`POST /v1/auth/exchange`).
Clients never assert their own roles or identities. Instead:
1. The client presents an upstream bearer token issued by the enterprise IdP (e.g. Okta, Auth0, Google Workspace).
2. OpsBlaze introspects the token to verify subject (`sub`), issuer (`iss`), audience (`aud`), and active status.
3. OpsBlaze matches `(tenant_id, idp_issuer, idp_subject)` in the PostgreSQL table `identity_membership` to derive the authoritative `actor_id` and authorized `roles`.
4. In production, password login is disabled.

## 2. Server Configuration

Ensure the following environment variables are configured in production:
```bash
OPSBLAZE_ENV="production"
AUTH_SECRET_KEY="<at-least-32-char-random-high-entropy-secret>"
POLICY_SIGNING_SECRET="<at-least-32-char-random-high-entropy-secret>"
AUTH_ALLOW_PASSWORD_LOGIN="false"

# Enterprise IdP Introspection Endpoint (RFC 7662)
IDP_INTROSPECTION_URL="https://idp.opsblaze.corp/oauth/introspect"
IDP_CLIENT_ID="opsblaze-production-client"
IDP_CLIENT_SECRET="<idp-client-secret>"
IDP_EXPECTED_ISSUER="https://idp.opsblaze.corp"
IDP_EXPECTED_AUDIENCE="opsblaze-core-api"
IDP_TIMEOUT_SECONDS="5.0"
```

## 3. Provisioning a New Member

To register an authorized employee or service principal for a tenant:

```bash
curl -X POST https://api.opsblaze.corp/v1/auth/memberships \
  -H "Authorization: Bearer <admin-bearer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "11111111-1111-4111-8111-111111111111",
    "idp_issuer": "https://idp.opsblaze.corp",
    "idp_subject": "auth0|64ab1234cdef5678",
    "actor_id": "alice.smith",
    "actor_kind": "HUMAN",
    "roles": ["release_owner", "operator"]
  }'
```

Response:
```json
{
  "id": "e4f8d21b-8521-49b0-a352-094191398188",
  "tenant_id": "11111111-1111-4111-8111-111111111111",
  "idp_issuer": "https://idp.opsblaze.corp",
  "idp_subject": "auth0|64ab1234cdef5678",
  "actor_id": "alice.smith",
  "actor_kind": "HUMAN",
  "roles": ["operator", "release_owner"],
  "is_active": true,
  "created_at": "2026-11-09T08:00:00Z"
}
```

## 4. Immediate (0-Second) Revocation Procedure

When an employee departs or credentials are compromised:

### Step 1: Deactivate in OpsBlaze
Deactivating the membership instantly terminates access across all active sessions:
```bash
curl -X POST https://api.opsblaze.corp/v1/auth/memberships/e4f8d21b-8521-49b0-a352-094191398188/deactivate \
  -H "Authorization: Bearer <admin-bearer-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "reason": "Security offboarding: contract concluded"
  }'
```

### Step 2: Verification of Immediate Revocation
Every authenticated API request synchronously checks `IdentityMembership.is_active` in PostgreSQL.
The very next request using an unexpired JWT previously minted for this user will fail immediately:
```bash
curl https://api.opsblaze.corp/v1/auth/me \
  -H "Authorization: Bearer <user-jwt>"
```
Expected output:
```json
{
  "detail": "Membership for actor 'alice.smith' is deactivated: Security offboarding: contract concluded"
}
```
HTTP Status: `401 Unauthorized`.

## 5. Troubleshooting & Diagnostics

- **401 Unauthorized on `/v1/auth/exchange`:**
  - Upstream token is inactive or revoked at the enterprise IdP.
  - Issuer mismatch: IdP token issuer does not match `IDP_EXPECTED_ISSUER`.
  - Audience mismatch: Token audience list does not contain `IDP_EXPECTED_AUDIENCE`.
  - Upstream token expired.
- **403 Forbidden on `/v1/auth/exchange`:**
  - Authenticated IdP subject has no active row in `identity_membership` for the requested `tenant_id`.
  - Contact the tenant platform engineer to provision membership.
- **400 Bad Request (SSRF Blocked):**
  - Configured `IDP_INTROSPECTION_URL` resolved to private IP, link-local metadata (`169.254.169.254`), or loopback in production.
  - Ensure public DNS resolves to valid enterprise IdP host.
