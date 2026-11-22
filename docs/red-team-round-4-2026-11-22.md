# OpsBlaze red-team round 4 — 22 November 2026

Scope: implementation-level failure modes in **G1 production identity
configuration**, **G2 external-adapter qualification evidence**, and
**G4 recovery-cost evidence**. This is a self-directed defensive code audit,
not an independent penetration test or a production release certificate.

## Finding 1 — G2 trace hash can endorse unsupported claims

**Before:** The offline qualifier verified a signed scenario summary and raw
trace SHA-256, but treated the trace file as opaque bytes. Any internally
consistent signature and hash could endorse unrelated or contradictory raw
content.

**Fix:** Use an exact, redacted JSONL event contract. Every trace now binds
one canonical effect identity and one original permit digest; consecutive
sequence numbers and causal phase ordering prevent mismatched histories.
Dispatch, applied-effect, status and independent oracle event counts must
match the reviewed scenario. Conservative outcome validation rejects false
verification, re-applied effects, missing failure observations and arbitrary
extra fields. The status-outage and status-recovery scenarios must refer to
the *same* effect and permit. Negative tests rehash corrupted traces to
exercise semantic checks rather than trivial digest mismatches.

**Residual:** An owner could still fabricate a completely self-consistent
transcript. G2 needs a provider-selected disposable target, externally
controlled raw traces and independently witnessed fault-injection results.
No external effect capability is activated.

## Finding 2 — G1 auth accepts ambiguous issuer/JWKS configuration

**Before:** The production trust gate compared JWKS and issuer hostnames,
but did not require the same HTTPS port or consistently reject query,
fragment and ambiguous encoded path components.

**Fix:** Strict same-origin checks, default-443 normalization, bounded URL
fields, no embedded credentials or query/fragment parameters and rejection
of ambiguous path components. The provider preflight shares the canonical
HTTPS port behavior. Negative tests verify malformed configurations
fail before a signing-key fetch can happen.

**Residual:** A syntactically good issuer/JWKS pairing is not a tested real
IdP. Signed token rotation, browser PKCE and revocation propagation
must be evaluated on the independently governed provider.

## Finding 3 — G4 multiple claimed recoveries reuse one witness

**Before:** A syntactically valid witness hash could be reused across many
VERIFIED observations, artificially inflating the aggregate recovery count.
Direct callers could also bypass the holdout-size/family checks.

**Fix:** Require unique witness references **across both arms**, strict
case identities, minimum family sizes and negative-control counts, and
report per-family recovery-per-cost regressions using exact integer
cross-products. Tests reject changed input even when the hash is recomputed,
while returning no per-case witness identities in the aggregate report.

**Residual:** A unique hash does not prove a real independent oracle. G4
requires actual protected trials, measured invoices, custody of labels,
separate evaluator authority and postcondition attestation.

## Release and verification boundaries

- Existing native PostgreSQL/Temporal, access control, security, CodeQL,
  frontend and release-candidate workflows must remain mandatory.
- GitHub-hosted provenance may occasionally fail because its upstream
  transparency-log service is unavailable. Retry attestation; **never**
  disable it or substitute a simulator PASS for the missing provenance.
- No Docker, new branches, PRs, real external effects, learned policy
  activation or downgraded G1–G6 acceptance requirements.
- G1–G6 stay BLOCKED pending genuine real-environment evidence and
  authorized independent reviewers.

The above documents implementation tests and residual risk; it does not
certify the code against every possible bug.

## Finding 4 — G2 trace file path could change after validation

**Before:** The reader checked `is_symlink()` and then separately opened
the file by pathname. A file substitution between the check and open
could redirect a privileged reviewer to an unintended local file.

**Fix:** Use Linux directory-relative `O_NOFOLLOW` opens and inspect the
opened file descriptor. Reject non-regular files, hard-linked traces and
redirected directories. Synthetic tests bypass the preliminary symlink
check or substitute hard links while leaving the content digest unchanged;
the new descriptor boundary still rejects them.

**Residual:** The external reviewer must protect the local evidence
directory and signing keys and obtain traces from a trusted provider
observer. This file-reader hardening is not itself provider attestation.
