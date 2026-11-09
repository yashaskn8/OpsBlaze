# Runbook: Isolated Candidate Shadow Evaluation Operations

## 1. Overview & Architectural Isolation

In Stage 11.2, releases in the `SHADOW` state evaluate candidate policy bundles against 100% live incident traffic under strict consequential isolation:
1. Live incidents are routed and served exclusively by the pinned INCUMBENT policy bundle.
2. An asynchronous outbox task `Outbox(kind="RELEASE_SHADOW_EVALUATION", dedupe_key="shadow:<release_id>:<incident_id>")` is transactionally enqueued.
3. The `ShadowOutboxDispatcher` claims rows and invokes `ShadowExecutor`.
4. `ShadowExecutor` verifies the cryptographic integrity (RFC 8785 + HMAC-SHA256) of both bundles, loads the exact evidence snapshot, and evaluates both arms in parallel.
5. **Zero Consequential Authority:** Candidate findings are never dispatched to B2 (resource reservation), B3 (effect receipts), P2 (approval cards), or A2 (mutation grants).
6. **Mid-Flight Kill-Switch Invalidation:** Before recording findings, `ShadowExecutor` executes a row-locked CAS check on `PolicyRelease`. If the kill switch was activated or the release was rolled back, findings are immediately discarded.

## 2. Promoting a Release to Shadow

A release owner or platform engineer promotes an approved release to `SHADOW`:

```bash
curl -X POST https://api.opsblaze.corp/v1/releases/<release_id>/transition \
  -H "Authorization: Bearer <release-owner-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "expected_version": 2,
    "target_state": "SHADOW",
    "comments": "Commencing 48-hour live traffic shadow evaluation"
  }'
```

Verify state:
```bash
curl -H "Authorization: Bearer <release-owner-token>" \
  https://api.opsblaze.corp/v1/releases/<release_id>
```
Expected output:
```json
{
  "id": "<release_id>",
  "state": "SHADOW",
  "canary_percentage": 0,
  "version": 3
}
```

## 3. Inspecting Shadow Divergence & Observations

Shadow observations are recorded in `release_event_audit` with `event_type = 'SHADOW_OBSERVATION'`:

Query divergence rate:
```sql
SELECT
  COUNT(*) AS total_shadow_evaluations,
  COUNT(*) FILTER (WHERE (metadata_redacted->>'divergence')::boolean = true) AS divergent_recommendations,
  ROUND(COUNT(*) FILTER (WHERE (metadata_redacted->>'divergence')::boolean = true)::numeric / COUNT(*)::numeric * 100, 2) AS divergence_percentage
FROM release_event_audit
WHERE release_id = '<release_id>'
  AND event_type = 'SHADOW_OBSERVATION';
```

Inspect sample divergent decisions:
```sql
SELECT
  created_at,
  metadata_redacted->>'incident_id' AS incident_id,
  metadata_redacted->>'candidate_action' AS candidate_action,
  metadata_redacted->>'incumbent_action' AS incumbent_action
FROM release_event_audit
WHERE release_id = '<release_id>'
  AND event_type = 'SHADOW_OBSERVATION'
  AND (metadata_redacted->>'divergence')::boolean = true
ORDER BY created_at DESC
LIMIT 10;
```

## 4. Emergency Kill Switch During Shadow Evaluation

If candidate divergence reveals alarming behavior or infrastructure degradation occurs, trigger the emergency kill switch:

```bash
curl -X POST https://api.opsblaze.corp/v1/releases/<release_id>/kill-switch \
  -H "Authorization: Bearer <commander-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "expected_version": 3,
    "justification": "Candidate proposes destructive replica pool failover on transient network blip"
  }'
```

### Safety Guarantees upon Kill-Switch Activation:
1. `PolicyRelease.kill_switch_active` is set to `true`.
2. Any pending shadow outbox items immediately abort with `SHADOW_ABORTED_KILL_SWITCH`.
3. In-flight shadow evaluations fail the mid-flight CAS lock recheck, discard candidate findings, and emit `SHADOW_RESULT_DISCARDED_KILL_SWITCH`.
4. Zero candidate findings enter metrics or influence future promotion gates.

## 5. Metrics & Monitoring

- `shadow_evaluations_completed_total`: Counter of successfully completed shadow evaluations.
- `shadow_evaluations_discarded_total`: Counter of evaluations discarded due to mid-flight kill-switch / rollback.
- `shadow_outbox_delivered_total`: Counter of delivered shadow outbox tasks.
- `shadow_outbox_dead_letter_total`: Counter of shadow tasks that exceeded max attempts.
