# OpsBlaze production acceptance gates

This is the executable team's release evidence checklist derived from the
112-page TITAN-20 proposal. **All gates are BLOCKED for real effects and live
policy promotion** until the listed externally witnessed evidence exists.
This repository's simulator CI is not deployment certification.

## G1 — Identity-provider integration and governance: BLOCKED

- Register a real provider with distinct API and security-event audiences,
  pinned HTTPS issuer/JWKS, RS256 access tokens and short-lived revocation
  events. Verify real browser authorization-code/PKCE redirects and callback.
- Run key rotation, token theft/replay, expired token, forged issuer/tenant,
  withdrawn role, stale reviewer approval and conflicting event-ID campaigns.
- Confirm that separate human reviewers cannot approve their own incidents,
  that revocation reaches all API replicas, and that only security authority
  can initiate identity revocation. Signed IdP events conservatively revoke
  the entire API identity.
- Record provider identity, tenant provisioning protocol, actual audit logs,
  cert/key rotation dates, security reviewer and independent penetration
  review. Do not commit tokens, subjects, or signing keys.

Implemented lab evidence: mocked RSA/JWKS selection and rotation, production
JWT scopes, explicit revocation deny-list, signed IdP event replay ledger and
pending-grant invalidation. An optional, credential-free **live** provider
preflight checks exact OIDC issuer and JWKS pins, same-issuer authorization
and token endpoints, authorization-code/PKCE S256 advertisement, RS256
metadata, and bounded RSA signing keys. Redirects and malformed/oversized
metadata fail closed. The repository cannot run this against an unspecified
identity provider; live issuance, browser callback and signed revocation
delivery remain untested until real provider access is supplied.

## G2 — External target adapter qualification: BLOCKED

- Select and authorize a particular provider, target family and disposable
  integration environment. Define one narrow effect action and separate
  read-only status/independent postcondition observers.
- Prove provider-side monotonic fencing or an equivalent **atomic** refusal
  contract, immutable effect identities, target-version compare-and-swap,
  documented idempotency retention and bounded retry semantics.
- Inject lost reply after commit, duplicate call, competing worker, old
  fence, external edit, partial apply, status-service outage, retention gap,
  and oracle failure. Record raw traces and the exact code/config digest.
- Require separate security and release owners before enabling a digest.
  No model, fixture PASS or local operator may grant an external capability.

Implemented lab evidence: seventeen disposable simulator scenarios. A separate
**offline G2 qualification-evidence verifier** now checks a frozen provider,
adapter, target, action, code/config/contract digest and bounded retention
window against 14 required external failure scenarios. It hashes each local
raw trace, rejects noncanonical or unstructured events, cross-checks event
counts against the scenario's signed result, and requires status outage
and recovery to bind the same original effect and permit. The output
does not publish raw trace content. Three distinct externally pinned
Ed25519 review signatures are required for provider witness, security
reviewer and release owner. This is source-and-signature integrity checking: a synthetic
file or a valid signature is **not** evidence of a real provider execution,
independent signer custody, provider-side fencing, or true idempotency
retention. No real adapter has been enabled; a green `LAB_PASS` or
`EVIDENCE_INTEGRITY_VALID_ONLY` is not provider certification.

## G3 — Multi-host recovery, restore and operational reliability: BLOCKED

- Deploy native PostgreSQL and persistent Temporal with independently
  managed backups; run dispatcher and activity workers across at least two
  physical/virtual hosts, not just two worker objects on one CI runner.
- Kill workers/dispatched RPC after PostgreSQL commit, after Temporal start,
  after effect SENT, after remote commit and before acknowledgement.
  Validate stable IDs, no blind retry, independent verification and recovery
  without duplicate logical effects.
- Restore PG and Temporal snapshots into an isolated network. Check revoked
  identities, consent epochs, effect receipts, workflow histories and
  migrations; require a security review before reopening mutation paths.
- Publish timestamped faults, raw logs, hardware/versions, RPO, RTO,
  throughput and p95/p99 under preregistered load, rather than estimates.

Implemented lab evidence: native Postgres/Temporal e2e, lost ACK tests,
history replay, and a two-worker handoff with parallel dispatcher calls.
Docker-free systemd service units now cover the API, outbox dispatcher,
Temporal incident worker, restricted projector, read-only aged-work monitor
and bounded retention timer. A CI source audit rejects unsafe service user,
credential-file, remote API bind, capability and restart changes. The API
can validate a passwordless projector role reference so only the projector
service needs the actual restricted database password. No unit has been
deployed, and these artifacts do not prove independent hosts.
The new opt-in per-host heartbeat ledger preserves each role's separately
reported host identifiers, rejects invalid identifiers and out-of-order
updates, and makes production readiness fail when fewer than two distinct
recently reported host IDs exist for either the dispatcher or incident-worker
role. Host IDs are self-reported through shared application database rights,
so two strings do **not** prove physically independent hosts or HA. G3
remains BLOCKED until independently inventoried host identities, fault
injection and raw recovery evidence are reviewed. Readiness now uses
indexed database-side fresh-host counts instead of scanning unbounded
retired host records in API memory. The bounded retention worker prunes
self-reported host rows older than seven days while preserving incident,
effect and approval records. Neither optimization certifies HA.
A CI-only PostgreSQL dump/restore rehearsal now verifies a synthetic
revocation/capsule sentinel, schema revision, completed outbox intent,
source RLS policies and re-applied restricted grants in an isolated database.
It proves revoked source rows stay hidden and a forged capsule insert is
refused. Additional synthetic restore sentinels ensure security-actor
revocation and IdP event replay metadata persist, while an UNKNOWN effect
receipt remains unresolved with its attempt and fence unchanged.
CI also checks that source-RLS rollback retains deny-by-default reads for
the old projector role. It does NOT restore Temporal persistence,
prove backup erasure, or establish multi-host HA.

## G4 — Protected independent held-out E5 evaluation: BLOCKED

- Commission an evaluation owner who is not the candidate author and
  controls sealed oracle outcomes. Preregister incident families, train/
  validation/holdout split, acceptance thresholds, sample size/stopping rule,
  primary **verified recovery per total cost**, safety regression bound
  and hardware/model versions before opening labels.
- Give each arm only blinded case inputs and equal tool permissions/budgets.
  Collect real outcomes including unknown/disputed results, status lookup,
  latency and token/provider costs; never omit invalid pairs from counts.
- Run negative controls, prompt-injection cases, model outages, memory
  ablations, routing baselines and per-family regression reporting.
- Require signed frozen manifests and independent review of result hashes.
  A wide/overlapping interval is INCONCLUSIVE, not a successful promotion.

Implemented lab evidence: smoke evaluation and an offline evaluator accepting
external protected oracle/trial files, with digest checks, negative controls,
cost, latency and conservative intervals. A new optional G4 signed
preregistration verifier checks an externally pinned Ed25519 evaluator key,
the immutable manifest digest, declared arm versions and an exact fixed
number of trial pairs. Optional externally supplied recovery-cost
observations are tied to the frozen oracle/trial digests and count all
UNKNOWN/DISPUTED outcomes and cost categories; negative safety controls
cannot count as recovered cases. Aggregate-only reports include the
verified-recovery-per-total-cost ratio and family regressions. Valid
signatures and synthetic measurements are integrity evidence only: an
external review still must prove who controlled the signing key, that
registration preceded oracle disclosure, independently witnessed recovery, auditable real provider/model/compute/
status-billing costs, separate reviewer authority and the real sample/
negative-control campaigns.
The offline evaluator **always** reports `promotion_eligible=false`.

## G5 — Signed human-controlled E6 release: BLOCKED

- Store immutable signed policy digests, exact E5 approval evidence and the
  frozen model/tool/lesson manifest. Add independent evaluator/reviewer and
  release-owner signatures, each checked at the action boundary.
- Enable only through a separately reviewed shadow → bounded canary →
  active transition, explicit abort thresholds, rollback timing, durable
  per-incident policy pins and audited exposure.
- Exercise concurrent admission/rollback, safety kill switch, controller
  restart, active-workflow policy pin, cohort skew, delayed labels, and
  accidental capability expansion. Unreconciled UNKNOWN effects abort.
- Keep the incumbent pinned if signer, evaluator, oracle, registry or
  rollout controller is unavailable.

Implemented lab evidence: immutable draft digests, deterministic bounded
cohort preview, persistent **shadow-only** observations, reviewer pause/
rollback and admission-time known-good pinning. An offline verifier now
requires **three distinct externally pinned Ed25519 keys** (evaluation owner,
security reviewer and release owner) to sign the same immutable policy
content digest, frozen E5 report digest, tool-manifest digest and git SHA.
It rejects altered or replayed content and same-key role impersonation, but
these deliberately non-authorizing signatures DO NOT prove real human role
custody, trusted time, host release checks, rollback readiness or a
production-capable E5 result. No candidate policy is assigned live.
`POST /v1/rollouts/{id}/approve` still rejects promotion.

## G6 — Cross-cutting production release: BLOCKED

- Independent security/tenant penetration testing, privacy review,
  retention/backup purge verification, UI keyboard/accessibility audit,
  load and multi-replica SSE validation, dependency/supply-chain review.
  The React workbench now has a keyboard skip-to-snapshot link, visible
  focus indications, scoped status announcements and individually named
  dead-letter actions. These source-level improvements and a successful
  frontend build do NOT substitute for assistive-technology/browser
  accessibility testing.
- Approvals and measured raw evidence must identify exact git commit,
  migrations, external adapter digest, data split, hardware and owner.
  Any change to a critical artifact invalidates its previous attestation.

**Release decision:** No claims of production-ready recovery, independent AI
improvement or safe live policy canary until G1–G6 are evidenced by the actual
environment and authorized owners. Never change a closed gate to PASS solely
because GitHub Actions is green.
