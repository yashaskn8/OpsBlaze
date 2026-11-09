# Runbook: Budgeted Strategy Routing & Decision Ledger Operations (Capability E7)

## 1. Overview & Architectural Invariants

Capability E7 (Budgeted Strategy Router) governs automated investigation routing under strict risk, budget, and capability constraints:

1. **Pre-Approved Route Catalog:** The router never selects arbitrary models or ad-hoc prompts. Dispatches are strictly confined to routes defined in PostgreSQL table `strategy_route`.
2. **Tool Capability Subsetting (Class 1 Invariant):** Strategy routes may only **narrow (subset)** the tool capabilities granted to an investigator; they can **never expand** permissions beyond the base catalog or grant mutation authority.
3. **Two-Phase Budget Reservation:** Pre-flight checks verify that the route's `max_cost_micros` does not exceed remaining tenant budget headroom. Post-execution, actual resource usage is settled.
4. **Deterministic Propensity Logging:** Every routing decision computes and logs selection propensity $P(\text{route} \mid \text{state}) \in (0, 1]$ in `strategy_decision` to support unbiased off-policy evaluation in S1 replay and E5 regression gates.
5. **Fail-Closed Conservative Fallback:** If budget is exhausted or risk ceilings are breached, the router deterministically falls back to `CONSERVATIVE_FALLBACK` or `HUMAN_ESCALATION` without throwing unhandled exceptions.

---

## 2. Standard Strategy Route Catalog

| Route Key | Model Tier | Permitted Tool Capabilities | Max Cost ($\mu\$$) | Max Tokens | Risk Ceiling |
|---|---|---|---|---|---|
| `FAST_HEURISTIC` | `flash_lite` | `["system_metrics", "log_query"]` | 2,000 | 1,024 | 0.25 |
| `STANDARD_PROBE` | `flash` | `["system_metrics", "log_query", "probe_read"]` | 10,000 | 4,096 | 0.60 |
| `DEEP_ANALYSIS` | `pro` | `["system_metrics", "log_query", "probe_read", "topology_inspect"]` | 50,000 | 16,384 | 0.85 |
| `CONSERVATIVE_FALLBACK` | `deterministic` | `["system_metrics"]` | 0 | 0 | 1.00 |
| `HUMAN_ESCALATION` | `human` | `[]` | 0 | 0 | 1.00 |

---

## 3. Discovering Available Strategy Routes

Query the active route catalog for the authenticated tenant:

```bash
curl -X GET https://api.opsblaze.corp/v1/strategies/routes \
  -H "Authorization: Bearer <operator-or-sre-token>"
```

Expected Response:
```json
[
  {
    "route_key": "DEEP_ANALYSIS",
    "model_tier": "pro",
    "allowed_tool_capabilities": ["log_query", "probe_read", "system_metrics", "topology_inspect"],
    "max_cost_micros": 50000,
    "max_tokens": 16384,
    "risk_ceiling": 0.85,
    "priority": 30,
    "is_active": true
  },
  {
    "route_key": "STANDARD_PROBE",
    "model_tier": "flash",
    "allowed_tool_capabilities": ["log_query", "probe_read", "system_metrics"],
    "max_cost_micros": 10000,
    "max_tokens": 4096,
    "risk_ceiling": 0.60,
    "priority": 20,
    "is_active": true
  }
]
```

---

## 4. Selecting a Strategy Route for an Incident

When an incident investigation starts, the workflow or operator requests route selection:

```bash
curl -X POST https://api.opsblaze.corp/v1/strategies/select \
  -H "Authorization: Bearer <operator-or-sre-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "incident_id": "8335e824-f9bd-4d1e-93f9-050090edef4b",
    "incident_severity": "high",
    "estimated_risk": 0.45,
    "requested_tools": ["system_metrics", "log_query", "probe_read"],
    "available_budget_micros": 25000
  }'
```

Expected Output:
```json
{
  "route_key": "STANDARD_PROBE",
  "model_tier": "flash",
  "granted_tool_capabilities": ["log_query", "probe_read", "system_metrics"],
  "propensity": 0.742158,
  "estimated_cost_micros": 10000,
  "is_fallback": false,
  "fallback_reason": null
}
```

---

## 5. Investigating Fallbacks & Operational Remediations

### Case A: Budget Exhaustion (`BUDGET_EXHAUSTED`)
- **Trigger:** `available_budget_micros` is less than the lowest-cost diagnostic route.
- **Router Behavior:** Automatically selects `CONSERVATIVE_FALLBACK` (cost = 0, deterministic status projection).
- **Remediation:**
  1. Inspect tenant budget headroom:
     ```sql
     SELECT tenant_id, SUM(cost_micros) AS spent_micros
     FROM budget_reservation
     WHERE tenant_id = '<tenant_id>' AND created_at > NOW() - INTERVAL '24 HOURS'
     GROUP BY tenant_id;
     ```
  2. If quota is exhausted due to high incident volume, grant an approved budget increase or wait for the quota window reset.

### Case B: Risk Ceiling Breach (`RISK_CEILING_BREACH`)
- **Trigger:** Incident uncertainty or impact score exceeds route ceilings (`estimated_risk > 0.80`).
- **Router Behavior:** Selects `HUMAN_ESCALATION`, halting automated inference and surfacing the incident in the P1 Live Investigation Workbench and P2 Human Cockpit.
- **Remediation:**
  1. Human incident commander investigates root cause manually using authorized tools.
  2. Once stabilized, mark incident resolved or adjust risk assessment parameters.

---

## 6. Decision Ledger & Propensity Auditing for Off-Policy Evaluation

Retrieve all recorded routing decisions for an incident:

```bash
curl -X GET https://api.opsblaze.corp/v1/strategies/decisions/<incident_id> \
  -H "Authorization: Bearer <operator-token>"
```

Inspect decisions directly in PostgreSQL:
```sql
SELECT
    id,
    route_key,
    model_tier,
    propensity,
    estimated_cost_micros,
    actual_cost_micros,
    fallback_reason,
    created_at
FROM strategy_decision
WHERE incident_id = '<incident_id>'
ORDER BY created_at DESC;
```

---

## 7. Post-Execution Cost Settlement

Once model inference and tool steps complete, settle the actual cost incurred:

```bash
curl -X POST https://api.opsblaze.corp/v1/strategies/decisions/<decision_id>/settle \
  -H "Authorization: Bearer <operator-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "actual_cost_micros": 3450
  }'
```

Verify that `actual_cost_micros` is updated, unblocking unreserved funds back to the tenant's budget pool.
