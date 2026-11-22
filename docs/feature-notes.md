# OpsBlaze implementation notes

These notes document implemented laboratory safeguards in addition to the
main README. They are **not** proof of production identity, model quality,
adapter certification, or policy-promotion eligibility.

The authorized operator stream at `GET /v1/incidents/{id}/stream` accepts
`after_id` and `Last-Event-ID`, replays up to 100 durable status events per
tick, sends keepalives, and closes after a bounded lifetime. The browser
uses an authenticated fetch stream (credentials never enter query strings),
reconnects with its last event ID, and falls back to JSON polling. Event
payloads are advisory only; the UI always refreshes the authoritative
versioned snapshot before acting. The SSE path remains a development-only
interface until production identity and load/concurrency testing exist.


Every new incident also pins `simulator-lab-route-v1` in
`policy_assignment` within the atomic admission transaction. Existing
incidents are pinned by migration 0019. E7 verifies that assignment before
routing and safely hands off when the pin is missing or changed. The policy
view is `GET /v1/incidents/{id}/policy`. Experimental E6 rollout drafts
still cannot assign themselves to incidents or enable real effects.


Workflow status projection is now an explicit phase-predecessor state machine.

Terminal `VERIFIED` requires `EXECUTING`, and repeated Temporal activity
delivery does not increment the version or duplicate UI events. Cancellation
is accepted only before the execution checkpoint; it never replaces an
in-flight effect with an untrue `CANCELLED` projection.


B4 admission now additionally enforces `OPSBLAZE_MAX_ACTIVE_INCIDENTS_PER_TENANT`
and `OPSBLAZE_MAX_ACTIVE_INCIDENTS_GLOBAL` by recounting durable
nonterminal incidents in PostgreSQL, under an admission transaction
advisory lock. Crash recovery does not depend on an in-memory counter,
and idempotent retries do not consume another slot. The global lock is
a simple lab baseline; measure contention before expanding worker
capacity. Expired/abandoned work requires B1 recovery or operator review,
not silent capacity forgiveness.


Run `python -m backend.workers.retention` as a one-shot maintenance
command to scrub at most 500 expired E1 capsules per invocation. It removes
their target digests and evidence references and revokes linked lessons in
the same transaction. Repeated runs are idempotent and leave minimal audit

IDs; no raw incident logs are added. Production retention scheduling and
backup/restore purge verification remain separate operational gates.


The CI backend job also runs a narrow Ruff undefined-name check
(`F821,F822,F823`) before tests. This intentionally avoids an
unrelated repository-wide style rewrite.


E5 reports now expose paired correctness wins/losses/ties, candidate
and incumbent probe-cost units, and a deliberately conservative

Hoeffding interval for paired binary correctness differences. If a
frozen trial pair is missing, report integrity becomes `INCOMPLETE`
without granting promotion. These are six public deterministic smoke
fixtures, **not** held-out production results: verified recovery per
cost remains explicitly `null` and promotion stays disabled.


Optional A1 model calls also reserve a conservative 512-token budget
against the tenant's durable fixed-minute counter before contacting the
provider. The cap is set by `OPSBLAZE_AI_TOKEN_LIMIT_PER_MINUTE` (default
2048). `GET /v1/operations/model-budget` shows reserved and provider-
reported usage; a failed or exhausted reservation falls back to the
deterministic investigator. This bounds the number of optional calls,
but a provider's actual prompt accounting may differ from the local
reservation, so it is not a billing guarantee.


E3 reviewer corrections now invalidate downstream E5 evaluations and E6
policy/rollout drafts in the same transaction as the fact-backed taxonomy
revision. Stale classification evidence can no longer leave a policy draft
presented as valid.


E3 classification now rejects older outcome revisions at ingestion and
requires a current, classified source case both when drafting and when
submitting an improvement candidate for evaluation. A later outcome
revision therefore blocks stale candidates even if a separate invalidation
worker has not yet updated the cluster projection.


The P2 execution gate also checks the **meaning** of the pinned observations:
the initial health must have been false, the version observation must match
the exact pinned integer, and the target must still be unhealthy. Rehashing
an invented healthy observation does not make it authorization evidence.


B4 now records an immutable `outbox.created_at` separate from the
retry lease `available_at`. The state-age monitor includes every
active incident phase (including EXECUTING) and overdue undelivered
intents even when a retry has been scheduled into the future.
`GET /v1/operations/queues` reports the oldest pending intent age
for the authenticated local tenant. Monitoring remains read-only and
never marks unresolved work as successful.


The native PostgreSQL integration suite also races five different incidents
for one simulator resource. Only one may hold the active fence, and a
superseded permit cannot apply an effect after a newer epoch is reserved.

This checks the database lock behavior instead of relying on SQLite mocks.

### Separate local reviewer credential (P2)


To approve or reject simulator plans through HTTP, also configure
`OPSBLAZE_REVIEW_TOKEN` with a different value than
`OPSBLAZE_LOCAL_TOKEN`. Supply it in the UI's local reviewer field.

The plan decision endpoint checks the second secret and persists
`decided_by=DEV_REVIEWER_CREDENTIAL` with the existing exact-plan
version and evidence checks. The operator token alone cannot decide
plans. These are still local shared secrets, **not real user identity**
or production separation of duties: production mode remains closed.


The same distinct local reviewer credential is also required before
the API accepts outcome feedback, failure-taxonomy corrections,
improvement submission, lesson validation/revocation, and rollout
pause/rollback. These checks prevent an operator-only token from
changing review decisions. As before, this is a developer-mode
capability boundary, not independent production identity or release
authorization.


A local plan approval also pins `OPSBLAZE_REVIEW_EPOCH` (default 1).

Increment this integer when revoking the entire local reviewer
credential set. The simulator gateway rechecks it before a new effect;
older approved plans fail closed to NEEDS_REVIEW. Existing effect
receipts remain reconcilable to avoid inventing outcomes. This is a
coarse developer-mode revocation checkpoint, **not** real per-user
revocation or an atomic distributed cancellation protocol.


P1 SSE now has a per-process concurrency cap of 32 streams. When it
is exhausted, new subscriptions receive 503 with Retry-After, and
their durable event cursor remains usable on reconnect. The
preflight query releases its database transaction before the
stream runs; each tick uses a short-lived read session. This
per-process limit is not a multi-replica load test or global quota.


## B3 durable effect permit binding

New simulator effect receipts persist the SHA-256 digest of the entire
original permit (tenant, incident, target, action, fence, expected version,
and expiry). Recovery status checks reject any applied-effect record with
a mismatched digest, tenant, target, or observed version. Pre-migration
receipts have a NULL permit binding and cannot be newly dispatched or
promoted to VERIFIED; they require manual review. The disposable adapter
conformance suite can still inspect direct synthetic effects without a
receipt. This is not an external adapter authorization.

Reconciliation also rechecks the applied-effect identity when an existing
receipt is CONFIRMED but its postcondition is not yet independently verified.
A missing status lookup leaves the receipt unresolved; an inconsistent
binding requires manual review. Internal state transitions refuse to dispatch
or verify legacy receipts without a valid digest.


## G4 frozen offline evaluation preregistration (not policy approval)

The protected E5 CLI accepts optional, **paired** inputs
`--preregistration` and `--trusted-evaluator-key`. The signed JSON record
must have exactly `schema=opsblaze-e5-prereg-v1`,
`manifest_sha256`, `signer_public_key_sha256`, `signed_at_utc` (UTC Z),
`expected_pairs`, `primary_metric=verified_recovery_per_total_cost`,
`stopping_rule=fixed_preregistered_sample`,
`arm_versions={candidate,incumbent}` and a canonical, unpadded
base64url Ed25519 `signature`. The signature covers the entire record
except the signature itself, using the explicit domain-separated bytes from
`backend.evaluation.preregistration.registration_bytes`.

The independent evaluator should supply and safeguard the private signing
key outside this repository. A separate review authority should supply
the trusted Ed25519 public key to the offline evaluator and authenticate its
fingerprint independently; never accept a public key supplied solely
inside a candidate's signed JSON. The signed date is **not** a trusted
timestamp; preregistration before running or unblinding trials requires
external time and custody evidence.

Example **verification only** (never promotion):

```bash
python -m backend.evaluation.heldout \
  --manifest frozen-manifest.json \
  --oracle protected-oracle.json \
  --trials blinded-trials.json \
  --preregistration signed-preregistration.json \
  --trusted-evaluator-key independent-owner-public.pem
```

A mismatched key/signature, altered manifest, malformed claims, or
a case count different from the signed `expected_pairs` causes an INVALID
result. Without both extra inputs the existing CLI remains explicitly
`UNSIGNED_LAB_ONLY`. Even a valid signature is not proof of a genuinely
independent evaluator, hidden labels, trusted timestamp, or real recovery.
Current E5 paired correctness does **not** yet compute verified recovery
per actual total cost. All reports retain `promotion_eligible=false`.


### Measured offline recovery-cost input

The same E5 offline CLI accepts `--recovery-measurements separate-outcomes.json`.
This file is supplied by an independent measurement/witness owner, never
by a candidate policy, and is bound to both `oracle_sha256` and
`trials_sha256`. It uses schema `opsblaze-e5-recovery-cost-v1`, with an
`arms` object containing exactly the candidate and incumbent records.
Every case of each fixed arm must have the exact fields
`case_id`, `outcome` (`VERIFIED`, `FAILED`, `UNKNOWN`, `DISPUTED`),
`witness_sha256` (lowercase 64-hex for VERIFIED, null otherwise),
`latency_ms`, and five integer nonnegative billing components:
`probe_microcredits`, `model_microcredits`, `provider_microcredits`,
`status_microcredits`, `compute_microcredits`.

The primary offline calculation is the count of witness-referenced
VERIFIED recoveries divided by the **sum of all five cost components
across all trials**, including unknown, disputed and failed outcomes.
A negative safety control is never counted as recovered. Outputs
include counts and costs by arm/family, a case-free aggregate,
and a ratio comparison using exact integer cross-products; duplicate,
missing, unbounded and malformed entries fail closed. Zero total cost
is invalid, not a free infinite improvement.

This input does NOT authenticate external witness independence or whether
the provided expense records correspond to real provider bills.
The witness SHA is a reference for independent review, not a proof of
correctness, and the verifier never outputs case-level oracle answers.
An unsigned lab dataset remains `UNSIGNED_LAB_ONLY`.
`promotion_eligible` is always false, irrespective of score.


## G5 three-authority frozen evidence review (OFFLINE ONLY)

`python -m backend.policy.release_evidence` validates three signed
`ACKNOWLEDGE_EVIDENCE_ONLY` records from separate pinned Ed25519
`evaluator`, `security_reviewer`, and `release_owner` public keys.
Each signed record binds the immutable policy content digest, the full
E5 report digest, the repository source commit (40 lowercase hex
characters), and a frozen tool-manifest digest (64 lowercase hex).
A role cannot reuse the same key as another role; wrong or missing
signatures, mismatched frozen evidence and payload changes are INVALID.

The command requires `--bundle`, `--evaluation`, `--reviews`, and
`--evaluator-key`, `--security-key`, `--release-owner-key`.
The bundle JSON must have exactly `policy_content`, `policy_digest`,
`source_commit` and `tool_manifest_digest`. The reviews JSON must be
an object containing one `reviews` array with precisely three objects.
Review signatures cover the entire review record except the signature
field, using `backend.policy.release_evidence.review_bytes`. Their
`signature` field uses canonical unpadded base64url encoding.

The E5 report must contain an existing `OFFLINE_PASS` and
non-promotable recovery measurements and a valid-signature
preregistration result. Even then, this code returns only
`REVIEW_SIGNATURES_VALID_ONLY` with
`external_role_custody_certified=false`, `promotion_eligible=false`,
and `deployment_authorized=false`. The signed time is only a claim;
a trusted independent authority must verify each human key's custody,
role, independence and timing before a future production release gate.
This script writes no registry or production state, and is never called
by the live admission, rollout or effect boundaries.


## G2 external-adapter qualification evidence (offline, not an adapter)

`python -m backend.adapters.qualification` verifies a separately collected
**disposable, authorized integration-environment evidence package**.
It never connects to the provider and is deliberately not registered with
OpsBlaze's effect gateway. Do not commit raw traces, tokens, private keys,
provider response bodies or identifying target data.

The operator supplies `--manifest manifest.json`, `--traces-dir traces/`,
`--reviews reviews.json`, `--provider-key provider-public.pem`,
`--security-key security-public.pem`, and
`--release-owner-key release-owner-public.pem`. Public keys must be
independently pinned outside the candidate's evidence bundle.

The manifest schema is `opsblaze-g2-qualification-v1` and requires a
specific non-simulator adapter, provider, disposable environment, target
family, effect action and source commit. Frozen SHA-256 digests bind the
adapter source, configuration, provider contract, status reader and separate
postcondition reader. A declared provider-side atomic refusal contract must
be either `ATOMIC_FENCE_AND_CAS` or
`PROVIDER_TRANSACTIONAL_REFUSAL`. The idempotency-retention window
must exceed the maximum declared retry window. These are signed claims
that still need independent verification against actual provider behavior.

The manifest records exactly 14 mandatory fault scenarios:
first apply, duplicate call, competing workers, stale fence, target-version
CAS, permit expiry, external edit, effect identity mismatch, lost response
after commit, status outage, independent-oracle outage, partial apply,
retention gap and status recovery. Each scenario fixes its allowed
conservative result, attempt count, applied-effect count and required
status/oracle observations. An uncertain effect must never be sent again
blindly. For every scenario `traces/<scenario>.jsonl` must exist and match
its pinned SHA-256; input is bounded to 2 MB per file. The tool reports
only aggregate proof metadata, not the private trace content.

Reviewers sign `REVIEW_EVIDENCE_ONLY` records using domain-separated
Ed25519 signatures. Three **distinct** externally pinned public keys are
required for `provider_witness`, `security_reviewer`, and
`release_owner`. The signed record binds the entire immutable manifest
digest, role, fingerprint and signing-time claim. The timestamp and role
are not independently certified by the signature alone.

Even with an internally consistent package, output is only
`EVIDENCE_INTEGRITY_VALID_ONLY` with `production_gate_passed=false`
and `external_effects_enabled=false`. G2 remains blocked until a named
real provider, externally witnessed tests, independently reviewed contracts,
actual retention guarantees and authorized release evidence exist. No
candidate AI, local operator or fixture may turn this report into a
production permission.
