# ADR-0014: Stage 12 — Capability E7: Budgeted Strategy Router & Propensity Attribution Ledger

* **Status:** Accepted (Standard Production Specification)
* **Date:** 2026-11-09
* **Authors:** OpsBlaze Architecture & Agentic AI Reliability Engineering
* **Capability Covered:**
  * **E7: Budgeted Strategy Router:** Select only pre-approved routes under risk and cost constraints, enforce server-side A2 tool capability subsetting, manage two-phase budget reservation via `DurableBudgetManager`, record deterministic propensity scores for unbiased off-policy S1/E5 evaluation, and enforce fail-closed conservative fallback.
* **Depends On:**
  * [`0001-b1-durable-incident-workflow.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0001-b1-durable-incident-workflow.md)
  * [`0002-b4-bounded-admission-and-stuck-state-monitor.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0002-b4-bounded-admission-and-stuck-state-monitor.md)
  * [`0005-a1-a2-investigator-contracts.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0005-a1-a2-investigator-contracts.md)
  * [`0006-a4-calibrated-abstention.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0006-a4-calibrated-abstention.md)
  * [`0010-stage9-candidate-incumbent-regression-gate.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0010-stage9-candidate-incumbent-regression-gate.md)
  * [`0011-stage11-e6-versioned-policy-canary-rollback.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0011-stage11-e6-versioned-policy-canary-rollback.md)
  * [`0013-stage11.2-production-identity-durable-guardrails-shadow-execution.md`](file:///c:/Users/prana/OneDrive/Desktop/OpsBlaze/docs/adr/0013-stage11.2-production-identity-durable-guardrails-shadow-execution.md)

---

## 1. Context and Problem Statement

In the OpsBlaze TITAN-20 architecture, incident investigation workflows must select appropriate model configurations and investigative depth dynamically. Different incidents present divergent risk profiles, urgency levels, and resource budgets. 

Prior to Capability E7, investigation strategies lacked centralized, budgeted routing:
1. **Unbounded Model & Cost Consumption:** An automated investigator could invoke expensive multi-modal or high-capacity models without validating remaining tenant token or dollar budget quotas, risking runaway costs or admission rejection.
2. **Privilege Escalation Risk:** If an LLM or prompt configuration could arbitrarily dictate which tools to execute, malicious or hallucinated plans could attempt to invoke mutation tools or sensitive diagnostic endpoints outside the authorized scope.
3. **Off-Policy Evaluation Impossibility (S1 & E5 Gaps):** In counterfactual paired replay (S1) and regression gating (E5), evaluating whether a candidate policy outperforms an incumbent requires known selection propensities $P(\text{route} \mid \text{state})$. Without explicit logging of route propensities and budget snapshots, off-policy importance sampling is mathematically invalid.
4. **Fragile Failure Handling:** If an incident encountered budget exhaustion or severe uncertainty, workflows could fail abruptly or attempt unsafe uncalibrated operations instead of deterministically selecting safe, bounded fallbacks or escalating to human operators (A4).

Capability E7 closes this final architectural gap by introducing the **Budgeted Strategy Router**.

---

## 2. Architectural Decisions & Technical Specification

### Decision 1: Pre-Approved Strategy Route Catalog (`strategy_route` Table)
The strategy router never invokes arbitrary models or ad-hoc configurations. All dispatch targets must originate from a verified catalog of pre-approved routes defined in PostgreSQL table `strategy_route`.

Each route specifies:
- `route_key`: Stable string identifier (e.g. `FAST_HEURISTIC`, `STANDARD_PROBE`, `DEEP_ANALYSIS`, `CONSERVATIVE_FALLBACK`, `HUMAN_ESCALATION`).
- `model_tier`: Model class (e.g. `flash_lite`, `flash`, `pro`, `deterministic`, `human`).
- `allowed_tool_capabilities`: Array of strings identifying permissible A2 read-only tools.
- `max_cost_micros`: Maximum cost in micro-dollars ($10^{-6}$ USD) permissible for this route.
- `max_tokens`: Maximum tokens allocated for investigation step.
- `risk_ceiling`: Upper bound on incident uncertainty or system impact score ($0.0 \le \text{risk} \le 1.0$) under which this route may be selected.
- `priority`: Integer evaluation priority (higher priority routes are evaluated first).
- `is_active`: Administrative enablement toggle.

Standard default catalog:
| Route Key | Model Tier | Permitted Tool Capabilities | Max Cost ($\mu\$$) | Max Tokens | Risk Ceiling |
|---|---|---|---|---|---|
| `FAST_HEURISTIC` | `flash_lite` | `["system_metrics", "log_query"]` | 2,000 | 1,024 | 0.25 |
| `STANDARD_PROBE` | `flash` | `["system_metrics", "log_query", "probe_read"]` | 10,000 | 4,096 | 0.60 |
| `DEEP_ANALYSIS` | `pro` | `["system_metrics", "log_query", "probe_read", "topology_inspect"]` | 50,000 | 16,384 | 0.85 |
| `CONSERVATIVE_FALLBACK` | `deterministic` | `["system_metrics"]` | 0 | 0 | 1.00 |
| `HUMAN_ESCALATION` | `human` | `[]` | 0 | 0 | 1.00 |

### Decision 2: Tool Capability Subsetting Invariant (Class 1 Invariant)
**A strategy route may only SUBSET (narrow) the tool capabilities granted to an investigator; it can NEVER EXPAND them.**

- Server-Side Enforcement: When preparing tool grants for an investigator session, the router intersects the route's `allowed_tool_capabilities` with the tenant's base tool allowlist:
  $$\text{EffectiveTools} = \text{TenantTools} \cap \text{RouteTools}$$
- If a route attempts to grant an unrecognized capability or any mutation capability, `StrategyPermissionEscalationError` is thrown, the route is quarantined, and the router fails closed.
- Under NO circumstances can E7 bypass B2 resource fencing, B3 effect receipts, or P2 approval cockpit rules.

### Decision 3: Two-Phase Budget Reservation & Settlement
Strategy selection integrates synchronously with `DurableBudgetManager` (`backend/budget/manager.py`):
1. **Pre-Flight Feasibility Check:** The router queries current budget headroom for the tenant.
2. **Pending Reservation:** Upon selecting a route, the router creates a pending budget reservation for `max_cost_micros` bound to the `incident_id`.
3. **Budget Exhaustion Handling:** If a route's `max_cost_micros` exceeds remaining tenant budget headroom, the route is deemed ineligible (`BUDGET_EXHAUSTED`). The router cascades to the next eligible route, ultimately selecting `CONSERVATIVE_FALLBACK` or `HUMAN_ESCALATION` if no budgeted route is feasible.
4. **Post-Execution Settlement:** After model invocation completes, the actual token usage and cost are settled against the pending reservation, returning unspent micro-dollars to the tenant quota.

### Decision 4: Deterministic Propensity Scoring & Attribution Ledger (`strategy_decision`)
For every routing decision, the router computes and records the exact selection propensity score:
$$P(\text{route}_i \mid \text{state}) = \frac{\exp(-\lambda_{\text{cost}} \cdot \bar{c}_i - \lambda_{\text{risk}} \cdot \bar{r}_i)}{\sum_{j \in \text{Eligible}} \exp(-\lambda_{\text{cost}} \cdot \bar{c}_j - \lambda_{\text{risk}} \cdot \bar{r}_j)}$$

Where:
- $\bar{c}_i$ is normalized route cost.
- $\bar{r}_i$ is route risk headroom.
- For fallback routes triggered by hard constraint violations (e.g. budget exhaustion), $P = 1.0$ conditioned on constraint breach.

The selection record is immutably appended to `strategy_decision`:
- `id` (UUID PK)
- `tenant_id` (UUID FK)
- `incident_id` (UUID FK to `incident`)
- `route_key` (VARCHAR(64))
- `model_tier` (VARCHAR(32))
- `allowed_tool_capabilities` (JSON)
- `propensity` (FLOAT)
- `budget_snapshot_json` (JSON)
- `estimated_cost_micros` (BIGINT)
- `actual_cost_micros` (BIGINT, nullable)
- `fallback_reason` (VARCHAR(64), nullable)
- `created_at` (TIMESTAMPTZ)

This enables mathematically unbiased off-policy evaluation, replay analysis (S1), and regression gating (E5) via inverse propensity scoring (IPS).

### Decision 5: Safe Fallback & Fail-Closed Protocols
- If no route satisfies both risk ceiling and cost budget constraints, the router deterministically falls back to `CONSERVATIVE_FALLBACK` or `HUMAN_ESCALATION` (`fallback_reason="RISK_CEILING_BREACH"` or `"BUDGET_EXHAUSTED"`).
- The router guarantees that no unhandled exception escapes to crash the incident workflow.
- Inactive routes (`is_active = False`) are completely excluded from consideration.

### Decision 6: API Layer & Strict Tenant Isolation
REST endpoints in `backend/api/strategies.py`:
- `GET /v1/strategies/routes`: Returns active strategy routes for the authenticated tenant.
- `POST /v1/strategies/select`: Executes budgeted route selection for an incident given its severity, risk score, and required tools. Requires `OPERATOR`, `RELEASE_OWNER`, or `SRE_LEAD` role. Enforces tenant match.
- `GET /v1/strategies/decisions/{incident_id}`: Retrieves decision history and propensity scores for an incident.
- `POST /v1/strategies/decisions/{decision_id}/settle`: Settles actual cost and token consumption post-execution.

---

## 3. Database Schema (Alembic Revision 0014)

```sql
CREATE TABLE strategy_route (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenant(id) ON DELETE CASCADE,
    route_key VARCHAR(64) NOT NULL,
    model_tier VARCHAR(32) NOT NULL,
    allowed_tool_capabilities JSON NOT NULL,
    max_cost_micros BIGINT NOT NULL CHECK (max_cost_micros >= 0),
    max_tokens INTEGER NOT NULL CHECK (max_tokens >= 0),
    risk_ceiling FLOAT NOT NULL CHECK (risk_ceiling >= 0.0 AND risk_ceiling <= 1.0),
    priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_strategy_route_tenant_key UNIQUE (tenant_id, route_key)
);

CREATE INDEX ix_strategy_route_tenant_active ON strategy_route (tenant_id, is_active, priority DESC);

CREATE TABLE strategy_decision (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    incident_id UUID NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    route_key VARCHAR(64) NOT NULL,
    model_tier VARCHAR(32) NOT NULL,
    allowed_tool_capabilities JSON NOT NULL,
    propensity FLOAT NOT NULL CHECK (propensity > 0.0 AND propensity <= 1.0),
    budget_snapshot_json JSON NOT NULL,
    estimated_cost_micros BIGINT NOT NULL,
    actual_cost_micros BIGINT,
    fallback_reason VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_strategy_decision_incident ON strategy_decision (tenant_id, incident_id, created_at DESC);
CREATE INDEX ix_strategy_decision_route ON strategy_decision (tenant_id, route_key, created_at DESC);
```

---

## 4. Threat Modeling (STRIDE) & Invariant Defense

| Threat Category | Potential Attack Vector | Router Mitigation & Invariant Defense |
|---|---|---|
| **Spoofing** | Attacker spoofs tenant identity to claim route or inspect decisions. | Server-enforced JWT principal validation via `get_authenticated_principal` and strict tenant matching. |
| **Tampering** | Rogue component modifies `allowed_tool_capabilities` to obtain mutation access. | Server-side intersection with tenant tool allowlist. Any non-read-only tool raises `StrategyPermissionEscalationError` and fails closed. |
| **Repudiation** | Operator denies route selection was automated. | Immutable append-only `strategy_decision` record with SHA-256 traceable context and propensity scoring. |
| **Information Disclosure** | Cross-tenant decision or route leakage. | Foreign key constraints and repository queries strictly filtered by `tenant_id`. |
| **Denial of Service** | Flooding router with complex incidents to exhaust LLM budget. | Pre-flight reservation against `DurableBudgetManager` rejects unbudgeted requests before dispatch. |
| **Elevation of Privilege** | Route attempts to grant B2 mutation fences or bypass P2 approval cockpit. | E7 is physically isolated from mutation authority; cannot mint fences or execute effects. |

---

## 5. Verification & Acceptance Criteria

1. **Unit Tests (`tests/unit/test_e7_strategy_domain.py`):**
   - Route catalog validation and capability constraints.
   - Deterministic propensity calculation and normalization.
   - Capability subsetting verification (cannot expand beyond base tools).
   - Cost and risk ceiling validation.
2. **Database Migration (`0014_stage12_e7_strategy_router.py`):**
   - Clean forward migration on both `opsblaze` and `opsblaze_test` PostgreSQL 17 databases.
   - Foreign key integrity and constraint verification.
3. **Integration Tests (`tests/integration/test_stage12_e7_strategy_router.py`):**
   - Full lifecycle: route catalog query -> budget check -> reservation -> selection -> decision ledger append -> settlement.
   - Cascade to fallback upon budget exhaustion.
   - Cascade to fallback upon risk ceiling breach.
4. **Security & Adversarial Tests (`tests/security/test_stage12_e7_security_adversarial.py`):**
   - Rejection of tool expansion attempts.
   - Zero-cross-tenant leakage.
   - Concurrency safety with multiple simultaneous route selections.
