# RUNBOOK: Authentication & Identity Operations

## 1. Overview and Authority Model

OpsBlaze enforces cryptographic bearer token authentication and server-derived identity across all API endpoints. In accordance with the TITAN-20 governance constitution:

- **AI models and automated agents are strictly non-human principals (`actor_kind: "AGENT"`).**
- **Models may never mint credentials, grant themselves roles, or self-approve plans or releases.**
- **All API requests must supply `Authorization: Bearer <token>` signed by the server's `auth_secret_key` using `HS256`.**
- **The deprecated `/v1/auth/token` endpoint permanently returns HTTP 401 Unauthorized.**

---

## 2. Preconfigured Principals and Role Directory

Authentication is performed via the `/v1/auth/login` endpoint using registered credentials from `backend/domain/auth.py`:

| Username | Default Password | Actor ID | Actor Kind | Primary Roles | Permitted Actions |
|---|---|---|---|---|---|
| `admin` | `AdminSecurePass2026!` | `admin-001` | `HUMAN` | `incident_commander`, `release_owner`, `platform_engineer`, `sre_lead` | Full administrative control, all release transitions, emergency kill switch |
| `release_lead` | `ReleaseLeadPass2026!` | `rel-lead-001` | `HUMAN` | `release_owner` | Approve releases, promote to shadow/canary/active, adjust canary percentages |
| `commander` | `CommanderPass2026!` | `cmd-001` | `HUMAN` | `incident_commander` | P2 plan approvals, emergency rollback, release kill switch |
| `sre_oncall` | `SRELeadPass2026!` | `sre-001` | `HUMAN` | `sre_lead` | Emergency release kill switch, emergency rollback |
| `platform_dev` | `PlatformDevPass2026!` | `dev-001` | `HUMAN` | `platform_engineer` | Draft policy bundles, create release candidates, view telemetry |
| `read_only` | `ReadOnlyPass2026!` | `audit-001` | `HUMAN` | `viewer` | Read-only telemetry, workbench, trace explorer, audit logs |
| `bot_investigator` | `BotAgentPass2026!` | `agent-a1-001` | `AGENT` | `operator` | Autonomous observation, hypothesis generation; **blocked from approvals & consequential effects** |

---

## 3. Standard Operating Procedures

### 3.1 Issuing a Session Token

To obtain a token for a human operator or automated service:

```bash
curl -X POST "http://localhost:8000/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "release_lead",
    "password": "ReleaseLeadPass2026!",
    "tenant_id": "00000000-0000-0000-0000-000000000001"
  }'
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "principal": {
    "actor_id": "rel-lead-001",
    "actor_kind": "HUMAN",
    "roles": ["release_owner"],
    "tenant_id": "00000000-0000-0000-0000-000000000001"
  }
}
```

### 3.2 Accessing Protected Endpoints

Pass the issued JWT in the HTTP Authorization header:

```bash
curl -X GET "http://localhost:8000/v1/releases/" \
  -H "Authorization: Bearer <token>"
```

If the `X-Tenant-ID` header is also provided, it must match the tenant ID encoded in the token claims. A mismatch results in `403 Forbidden`.

### 3.3 Revoking a Token

If a token is suspected of compromise or an operator session terminates:

```bash
curl -X POST "http://localhost:8000/v1/auth/revoke" \
  -H "Authorization: Bearer <token>"
```

The token's unique ID (`jti`) is added to the in-memory `TokenRevocationStore`. Any subsequent requests using that token will fail immediately with `401 Unauthorized: Token has been revoked`.

---

## 4. Separation of Duties and Release Governance

The `RELEASE_ROLE_MATRIX` strictly governs who can execute release actions:

```
+-----------------------------------------------------------------------------------+
| Action               | Permitted Roles                                            |
+----------------------+------------------------------------------------------------+
| bundle_create        | platform_engineer, release_owner                           |
| release_create       | platform_engineer, release_owner                           |
| release_approve      | release_owner, incident_commander (drafter_id != actor_id) |
| promote_shadow       | release_owner, platform_engineer                          |
| promote_canary       | release_owner, platform_engineer                          |
| adjust_canary        | release_owner, platform_engineer                          |
| promote_active       | release_owner, platform_engineer                          |
| rollback             | incident_commander, release_owner, platform_engineer, SRE  |
| revoke               | incident_commander, release_owner, platform_engineer, SRE  |
| kill_switch          | incident_commander, sre_lead, release_owner                |
+-----------------------------------------------------------------------------------+
```

### Constraint: Drafter Cannot Approve
When a release candidate is created by an engineer, the database enforces `drafter_id <> reviewer_id`. A release drafter attempting to approve their own release candidate is rejected with `400 Bad Request: Drafter cannot approve own release`.

---

## 5. Emergency Incident Procedures

### 5.1 Suspected Credential Compromise
1. Identify the compromised principal's `actor_id`.
2. Execute immediate revocation on active tokens via `/v1/auth/revoke`.
3. If secrets themselves are compromised, rotate `OPSBLAZE_AUTH_SECRET_KEY` in environment configuration and restart backend processes. (All existing tokens will fail signature verification).

### 5.2 Malfunctioning Agent Policy / Active Canary Breach
1. Authenticate as `sre_oncall` or `commander`.
2. Trip the emergency release kill switch:
   ```bash
   curl -X POST "http://localhost:8000/v1/releases/{release_id}/kill-switch" \
     -H "Authorization: Bearer <sre_token>" \
     -H "Content-Type: application/json" \
     -d '{"reason": "Emergency trip: abnormal latency spike observed"}'
   ```
3. Verify routing has immediately reverted 100% of incident traffic to the incumbent baseline.
4. Consequential B2 resource reservations and P2 plan approvals will automatically fail-close with HTTP 403.

---

## 6. Troubleshooting

| Error Code | Error Detail | Root Cause & Resolution |
|---|---|---|
| `401 Unauthorized` | `Token has been revoked` | Token JTI is marked revoked. Operator must re-authenticate via `/v1/auth/login`. |
| `401 Unauthorized` | `Invalid authentication credentials` | Invalid signature, expired token, or non-HS256 algorithm. Re-issue token. |
| `401 Unauthorized` | `Client-asserted token issuance is permanently disabled` | Caller hit deprecated `/v1/auth/token`. Use `/v1/auth/login` with registered credentials. |
| `403 Forbidden` | `Role 'operator' lacks authority for 'promote_active'` | Principal lacks required role in `RELEASE_ROLE_MATRIX`. Use credentials with `release_owner` role. |
| `403 Forbidden` | `Tenant header mismatch` | The `X-Tenant-ID` header does not match the token's `tenant_id` claim. Remove the header or supply matching tenant ID. |
| `403 Forbidden` | `Autonomous agents may not review or approve plans` | Agent principal attempted to approve a P2 plan. Human reviewer must sign approval. |
