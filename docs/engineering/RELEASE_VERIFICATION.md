# Release Verification & Decision Summary

## TITAN-20 v2.0 dual-profile architecture

Design baseline `c88065af312e2d2ea52150e6b47e1e69c9baaed8` passed [all ten Linux CI jobs](https://github.com/yashaskn8/OpsBlaze/actions/runs/46041260659). The original 112-page blueprint and its SHA-256 remain the historical source. The release profile is a statement of supported scope. The existing authentication, tenant isolation, approval grant, adapter and policy-promotion checks remain their own authorities.

Community Edition uses the existing native single-host simulator, PostgreSQL, Temporal and React application. The runtime accepts the `community` profile and serves its scope at `/release-profile`. The artifact manifest is generated from the same backend module. The existing `development` and `production` environment settings still govern their current authentication and operational checks, including production-mode refusal when IdP configuration is missing. Enterprise selection fails at settings validation. Enterprise requirements and gates retain the existing external acceptance contract.

Run `python scripts/release_requirements.py --summary` for the record-derived totals. The full report includes the 176 historical blueprint anchors and the [269 OpsBlaze Omega records](../requirements-omega.json), including all 193 original children and their traceability. Each Omega row preserves the historical source and shows applicability, verification and dependencies by profile. The import checked all 269 requirement texts, acceptance criteria, classifications, priorities and verification methods against the companion checklist. The JSON records the SHA-256 of both inputs.

Record-level Community applicability after the sole authorized exclusion is 224 mandatory, 13 conditional, 8 recommended and 23 outside scope. The source's project summary listed 12 conditional and 24 outside scope. `AI-EVAL-07` is conditional and currently inactive because the grader is deterministic. Seven Enterprise-mandatory rows are recommended in Community. Historical G1–G6 dependencies remain in Enterprise. Community uses the capability references and retains explicit local/manual acceptance conditions on the corresponding atomic rows.

`SH-REL-01` remains visible as `USER_AUTHORIZED_SCOPE_EXCLUSION` and contributes no verification or acceptance credit. The candidate carries no redistribution claim. For an artifact-bound decision, run `python scripts/release_requirements.py --decision --source-commit <full-sha> --artifact <candidate.tar.gz>`. The command checks the archive's source marker and hashes the completed archive. CI uploads the decision beside the archive and checksum. The inventory assembled inside the archive is historical verification context. Zero Omega requirements have formal acceptance recorded.

New engineering evidence records include the verified commit and covered repository paths. When assembling an exact-commit inventory, the release workflow checks the history between the verified and candidate commits. A later change to a covered path, including a reverted change, marks the record `STALE`. A missing verifiable history also marks it stale. The report retains stale records for traceability and withholds their current verification credit. Use `--source-commit` with a checkout that includes the relevant history to run the same check locally.

| Community gate | Current evidence and decision |
|---|---|
| C1 Local identity and authorization | Local API, tenant isolation, distinct review credentials and refusal tests. Community acceptance pending validation of the packaged installation and operator session procedure. |
| C2 Simulator conformance | Native conformance and effect/reconciliation integration evidence. Candidate evidence is generated in the existing native CI job. |
| C3 Native reliability | Native PostgreSQL migrations, paired PostgreSQL and Temporal cold restore, Temporal worker restart and history replay. A fresh packaged installation under native supervision still requires acceptance evidence. |
| C4 AI evaluation | The deterministic comparisons and native memory ablation retain negative and inconclusive results. Model-assisted end-to-end measurement requires separate model-run evidence. |
| C5 Shadow rollout governance | Shadow assignment, simulator canary and rollback have repository tests. Live promotion is disabled. |
| C6 Community release quality | Existing automated build, security, dependency and browser gates apply. Actual spoken output for approval, rejection, expiry and conflict remains pending manual acceptance. |

Community decision remains **pending qualification**. Enterprise decision remains **blocked** under the six gates below. The build produces a candidate archive, profile and requirement reports, Python and npm dependency inputs, source commit, SHA-256 manifest and GitHub build provenance. The release artifact applies the same prerequisites as the existing simulator candidate job.

### Executed candidate evidence and limitations

- GitHub's private vulnerability reporting setting was enabled and read back as enabled for `yashaskn8/OpsBlaze`. The [security policy](../../SECURITY.md) links to that channel and defines supported versions, the acknowledgement target and coordinated disclosure. A report-delivery exercise remains pending.
- The Python 3.12 candidate uses the [build lock](../../requirements-build.lock) for the build frontend and backend and the [runtime lock](../../requirements-runtime.lock) for transitive runtime, AI and observability packages. CI requires SHA-256 matches and wheels for both installations, builds with the locked backend and checks the installed candidate. A fresh Windows environment built the distribution, installed the wheel and reported no broken requirements. The negative control refused an intentionally incorrect SHA-256. The combined locked-package audit reported no known vulnerabilities. Regenerate the lockfiles using the commands in their headers.
- The release build creates a CycloneDX 1.6 `SBOM.cdx.json` from the Python runtime pins and npm's production dependency inventory. It preserves Python platform markers, checks the npm component list against `package-lock.json`, validates the final document against the CycloneDX schema, and adds its digest to `SHA256SUMS`. The archive contains the same SBOM as the separately downloadable copy.
- The [Omega Linux CI candidate for `01afb3656d7acec8ad096fd1bf9130e07e79538d`](https://github.com/yashaskn8/OpsBlaze/actions/runs/46049377476) passed all ten jobs. The downloaded archive SHA-256 `843c8026babb21054c2e9519d07ee6aa6ce8b80a3bc6bec23b3f8dcbeb1c91fb` matched both `SHA256SUMS` and `RELEASE_DECISION.json`. The downloaded native restore record named the same source commit and confirmed Temporal history and outbox recovery.
- Verify an exact GitHub-hosted candidate with `gh attestation verify <candidate.tar.gz> --repo yashaskn8/OpsBlaze --signer-workflow yashaskn8/OpsBlaze/.github/workflows/ci.yml --source-ref refs/heads/main --source-digest <full-commit> --deny-self-hosted-runners`. This command passed for the Omega archive above.
- The Windows Community runtime tests verified startup refusal of both excluded configurations, generation of distinct tokens in owner-only files, refusal to overwrite a credential file, and rejection of the old operator token after rotation. Playwright exercised the built workbench on loopback, confirmed keyboard focus at the incident snapshot, checked the simulator-only metadata and verified refusal of an unauthenticated API request.
- The local Windows archive smoke used source `556088d0391d608f6f191150b20b82c492e9f504`. The archive SHA-256 was `f0e4395df98d75d11195de99229d641eed8637a087ade723a657fd947232458b`. A clean extraction and isolated wheel install started the loopback launcher, served the built frontend and profile manifest, and refused an unauthenticated incident request. The archive digest belongs to that local build. GitHub generates separate provenance for its exact artifact.
- The [test evidence ledger](TEST_EVIDENCE.md) records native PostgreSQL restore, Temporal replay and Windows regressions. The [performance ledger](PERFORMANCE_BASELINE.md) records the synthetic workload and model comparison limitations.
- The paired restore ran on Windows with PostgreSQL 16.16 and Temporal CLI 1.10.1 on the Omega working tree based on `49b8a4c8470f0ecf019ff98dc0ceb8bc248588f2`. It restored both stores into separate destinations, retained the original workflow run and start history, and acknowledged the restored outbox without creating another workflow. Restored projector isolation and denied capsule insertion passed. Linux systemd execution, release-specific load objectives, measured optional model costs and spoken-output acceptance still need evidence.
- Community shared tokens support a trusted single-host lab. Rotate local credentials and clear browser sessions as needed. Individual remote identity revocation remains an Enterprise concern. The npm lockfile fixes the frontend dependency resolution.
- The 20-capability [CodeGraph runtime review](FEATURE_UPGRADE_LEDGER.md#codegraph-runtime-review) and the [architecture gap ledger](ARCHITECTURE_GAP_LEDGER.md) record targeted runtime and fault-path reviews. A fresh file-by-file audit and independent qualification remain open.

## Historical simulator verification

- **Repository**: `https://github.com/yashaskn8/OpsBlaze` (`main`)
- **Authoritative Blueprint**: `TITAN20_MASTER_PRODUCTION_ARCHITECTURE_AGENTIC_AI_BLUEPRINT_2027.pdf` (SHA-256 `135aa2b7333a1d6869193372f29ae24cae4d995705b6540e679f4b32cfd5ad87`, 112 pages)
- **Evaluated Commits**: `7c2d567` (reconciled audit/ledger CI [45848362323](https://github.com/yashaskn8/OpsBlaze/actions/runs/45848362323) — all 10 Linux jobs `success`), `b0984ad` (HTTP security headers middleware, B2 canonical lock order + lease renewal, test fixes), and the Phase 3–4 documentation/constraint commit at HEAD.

## 1. Local Release Gate Status ([CONSTRAINTS.md](../../CONSTRAINTS.md))

| Gate | Status | Evidence |
|---|---|---|
| Migrations (`0054_evidence_lifecycle`) & zero drift | `PASS` | `scripts/check_migrations.py`, PG16 `alembic check` + downgrade/upgrade |
| Tracked secrets & supply-chain deps | `PASS` | `scripts/check_secrets.py` (`288` paths), `pip check`, `npm audit --omit=dev` (`0` vulns), Bandit `-ll` (exit `0`) |
| Python & TS architecture boundaries | `PASS` | `lint-imports` (`6 kept, 0 broken`), `depcruise` (`0` violations), `codegraph cycles` (`[OK] No circular imports`) |
| HTTP auth matrix, OpenAPI & B2 fencing | `PASS` | `73 passed in 15.58s` across Phase 1 suite including `test_openapi_contract.py` & `test_b2_reservation_invariants.py` |
| Native PG16 restore drill & Temporal replay | `PASS` (lab) | `pg_restore_smoke.py verify` (`PASS`), `9 passed` Temporal suite |
| Frontend tests & production build | `PASS` | `17/17` Node tests, `tsc --noEmit && vite build` |
| Full Windows backend suite + Linux CI | Executed in Phase 4 | Verified locally (minus 9 POSIX exclusions) & reconciled against exact-commit Linux CI |

## 2. Production Release Gates (`G1`–`G6`)

| Gate | Status | Blocking Requirement |
|---|---|---|
| **G1** IdP integration & governance | `BLOCKED` | Real external OIDC IdP, live PKCE callback, real multi-replica revocation |
| **G2** External target adapter | `BLOCKED` | External provider qualification, real fencing, real retention TTL (`>= G2`) |
| **G3** Multi-host HA & restore | `BLOCKED` | Physically separate hosts, fault campaign, real RPO/RTO witness |
| **G4** Independent held-out E5/E4 | `BLOCKED` | Independent sealed oracle custody, real model/provider costs |
| **G5** Signed human E6 rollout | `BLOCKED` | Separate evaluator, security reviewer, and release owner Ed25519 sign-off |
| **G6** Cross-cutting & manual AT | `BLOCKED` | External pentest, privacy sign-off, manual Narrator spoken-output AT (`P2-08`) |

## 3. Final Engineering Verdict

- **Local Automatable Software Completeness**: **`VERIFIED` / `154 VERIFIED`, `2 PARTIAL` (`P2-08` manual AT, `E4-07` external G4 efficacy), `20 EXTERNALLY BLOCKED`** (`176` total in [docs/requirements-audit.json](../requirements-audit.json)).
- **Production Release Decision**: **`BLOCKED`** — Simulator-only controls, local development tokens, and non-promotable evaluation gates remain strictly enforced until externally witnessed G1–G6 evidence is supplied.
