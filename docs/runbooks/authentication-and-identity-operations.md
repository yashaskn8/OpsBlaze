# RUNBOOK: Authentication & Identity Operations

## 1. Current authentication boundary

- Protected API routes require an HS256 bearer token verified by the backend.
- `/v1/auth/token` is permanently disabled.
- The built-in password registry is a development/test fixture only. The `/v1/auth/login` endpoint returns HTTP 503 outside `development` and `test`.
- Production login is intentionally unavailable until a trusted identity provider and authoritative user-to-tenant membership store are integrated. Do not bypass this restriction by changing the environment label.
- Development identity names shown in the React UI are labels only. Operators must enter credentials; no passwords are shipped in the frontend bundle.
- Browser access tokens are held in React memory only. A page reload requires a new sign-in.

## 2. Database migration requirement

Token revocation is persisted in PostgreSQL in the `revoked_token` table. Apply migrations before starting the API:

```powershell
uv run alembic upgrade head
```

The migration is additive and creates the revocation table and expiry index. It does not drop or rewrite existing incident, approval, release, or tenant data.

## 3. Development sign-in

The development registry in `backend/domain/auth.py` exists solely for local and automated test environments. It is not an employee directory, tenant membership service, or production credential store.

The development endpoint currently accepts a tenant identifier after verifying that the tenant exists. This is not adequate production tenant authorization; production login remains disabled until user-to-tenant membership is backed by an authoritative source.

Never deploy the development registry or its shared fixture credentials as a staging or production identity mechanism.

## 4. Token revocation

A principal can revoke only the bearer token used for the current request:

```powershell
$headers = @{ Authorization = "Bearer <current-token>" }
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/v1/auth/revoke" -Headers $headers -ContentType "application/json" -Body "{}"
```

The API stores the token JTI, actor, tenant, and signed expiry in PostgreSQL. All application workers consult the same database record. Revoked tokens are rejected with HTTP 401 until their expiry. A caller-supplied JTI that does not match the current token is rejected with HTTP 403.

Revocation depends on migration `stage11_1_001_revoked_tokens` being applied. The process-local `TokenRevocationStore` is retained only as a test compatibility helper and is not the runtime persistence mechanism.

## 5. Authorization and release governance

- Tenant-scoped APIs derive tenant identity from the verified principal; `X-Tenant-ID` is only a consistency check.
- P2 reviewer identity and role are derived from the verified principal at the API boundary.
- Bots and models cannot receive human approval authority.
- E6 mutating routes use the release role matrix.
- B2 reservation and P2 approval/dispatch must verify the pinned release state before consequential operations.
- Release states other than the explicit permitted states fail closed for candidate consequential activity.

## 6. Known limitations — do not represent these as accepted

1. No production OIDC/OAuth identity provider is currently integrated.
2. No authoritative production user-to-tenant membership directory is currently integrated.
3. The shadow observation method currently records an audit observation; it does not execute candidate reasoning. It must not be described as full shadow evaluation.
4. Runtime guardrail evaluation is currently triggered after terminal-state persistence and logs/swallow failures. It is not yet a durable retryable outbox workflow.
5. The current development identity fixture is not suitable for shared or production use.

## 7. Verification checklist

Before any production deployment:

- Configure a real identity provider and validate issuer, audience, signature, expiry, and user identity.
- Resolve tenant membership from an authoritative source, never from the login request alone.
- Remove the development identity registry from production configuration.
- Apply PostgreSQL migrations through the approved deployment pipeline.
- Verify token revocation across process restarts and multiple workers.
- Complete durable guardrail scheduling and real isolated shadow execution.
- Review the updated Stage 11.1 acceptance report and current GitHub Actions results.


## 8. Production identity-provider integration (OAuth2 token introspection)

OpsBlaze supports a provider-neutral RFC 7662 token-introspection boundary. Production does not trust a client-supplied subject, tenant, actor kind, or role. The configured identity provider authenticates the upstream access token; OpsBlaze then resolves the exact issuer + subject + requested tenant against the server-managed `identity_membership` table.

Configure these environment variables through the deployment secret manager:

- `IDENTITY_ISSUER`: exact HTTPS issuer identifier returned by the provider.
- `IDENTITY_INTROSPECTION_URL`: provider's HTTPS RFC 7662 introspection endpoint.
- `IDENTITY_CLIENT_ID`: confidential OpsBlaze API client identifier.
- `IDENTITY_CLIENT_SECRET`: confidential client secret; never commit or log it.
- `IDENTITY_AUDIENCE`: expected resource-server audience in the introspection response.

The API exchanges the provider access token at `POST /v1/auth/exchange` with JSON fields `access_token` and `tenant_id`. The requested tenant is accepted only when an active server-managed membership exists for the verified issuer, subject, and tenant. The returned OpsBlaze bearer token has a 15-minute lifetime and its revocation is persisted in PostgreSQL.

### Provisioning tenant membership

Only a trusted deployment administrator may provision or revoke identity memberships. Roles are server-owned data, not claims accepted from the browser. Example SQL (replace values with verified provider claims and a real tenant UUID):

```sql
INSERT INTO identity_membership
    (id, issuer, subject, tenant_id, actor_id, actor_kind, roles, is_active)
VALUES
    (gen_random_uuid(),
     'https://issuer.example/',
     'provider-stable-subject',
     '00000000-0000-4000-8000-000000000000',
     'ops-user-123',
     'HUMAN',
     '["operator"]'::json,
     TRUE);
```

Grant release or evaluation roles only after the organization's identity governance process approves the assignment. Use the provider's immutable subject identifier, not an email address that can change. For a user with access to multiple tenants, create one row per authorized tenant.

The provider-neutral exchange is implemented, but production identity is not operational until the organization selects and configures its provider, registers the confidential client, verifies introspection behavior and issuer/audience claims, provisions membership rows, and performs an end-to-end test. Password-registry login remains disabled outside development/test.
