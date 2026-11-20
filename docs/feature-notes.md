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
