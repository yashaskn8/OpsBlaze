# Frontend reconstruction

## Research before implementation

| Reference | Observed pattern | OpsBlaze adaptation |
| --- | --- | --- |
| [Linear Triage and views](https://linear.app/docs/triage) | Intake and a focused list support the next decision | Separate intake from the incident list and keep keyboard operation native |
| [Grafana Explore](https://grafana.com/docs/grafana/latest/visualizations/explore/get-started-with-explore/) | Compare related data without losing current context | Keep a stable incident identity while moving between investigation and source records |
| [Sentry issue details](https://docs.sentry.io/product/issues/issue-details/) | Incident summary and diagnostic breadcrumbs serve different readers | Distinguish claims, evidence, durable events and witnessed outcomes visually |
| [incident.io timeline and activity log](https://incident.io/changelog/easier-alert-source-set-up) | Curated timelines and complete activity logs answer different questions under explicit roles | Put review against the exact plan next to the incident context and keep durable history separate |
| [IBM Carbon data tables](https://www.carbondesignsystem.com/building-blocks/core/components/data-table/accessibility) | Native cell controls follow ordinary keyboard navigation | Keep rows readable and use a named native button to open an incident |
| [Datadog Events Explorer](https://docs.datadoghq.com/events/explorer/navigate/) | Details remain tied to the selected event | Keep one authoritative source-record inspector and expose missing references |
| [PatternFly page](https://www.patternfly.org/components/page/) | Technical applications distinguish navigation, masthead and content | Reflow those landmarks without duplicating controls |
| [GitHub compare](https://docs.github.com/en/pull-requests/committing-changes-to-your-project/viewing-and-comparing-commits/comparing-commits) | A comparison names both inputs and shows their differences | Put both replay arms in a shared record table and preserve the seed and fixture |
| [Vercel Runtime Logs](https://vercel.com/docs/logs/runtime) | Diagnostic rows separate level, time and details | Keep queue failures and incident event history explicit |

The previous workbench was a single scrolling page with equal card treatment for unrelated tasks. Its connection indicator could display synchronization before any incident was selected. Baseline `0f56cd8` built successfully at 339.88 kB JS and 5.02 kB CSS (100.26 and 1.70 kB gzip), with 17 Node tests passing. Linux CI run [46053679635](https://github.com/yashaskn8/OpsBlaze/actions/runs/46053679635) passed all ten jobs.

## Functionality preservation

| Existing control or data | API consumer | Authority | Redesigned location and verification |
| --- | --- | --- | --- |
| Sign in, clear session and local credentials | OIDC redirect and `/v1/auth/me` | Existing identity and tenant verification | Workspace intake. Check named controls and role-gated actions |
| New incident and request key | `POST /v1/incidents` | Operator with tenant and idempotency key | Intake form. Submit against the disposable API |
| Refresh, choose and list | Incident GET endpoints | Tenant read | Snapshot and recent register. Check selected row and refresh |
| Cancellation, acknowledgement and handoff | Incident mutation endpoints | Operator and current snapshot | Snapshot actions. Existing API regression and browser review |
| Investigation, stream and event history | Investigation, event page and SSE | Tenant read | Incident detail and durable history. Verify live refresh |
| Exact plan approval, rejection and revocation | Plan decision and approval endpoints | Reviewer plus existing approval checks | Review section. Check version, reason, state, focus and errors |
| Evidence paging, lineage and source inspection | Evidence page, lineage and record requests | Tenant read | Evidence and trace workspace. Verify actual evidence and missing-state copy |
| Queue refresh | Operations queues | Tenant read | Queue health. Read API values and distinguish missing data |
| Dead-letter load, acknowledge and redrive | Operations dead-letter endpoints | Existing tenant, operator and security administrator rules | Experience review. Check gated controls and empty result |
| Paired replay | Experiments and results | Operator | Replay lab. Run a paired fixture and compare both arms |

## Design tool record

| Installed skill | Invocation and result |
| --- | --- |
| Impeccable 4.11.1 | `impeccable.cmd context --target frontend` exited 1 because engine 0.1.31 could not create its default cache directory. The launcher verifies downloaded engine checksums, and the optional hook runs outside the agent sandbox. The engine and hook were left inactive. The installed init, shape, critique, audit, distill, layout, typeset, clarify, quieter, harden, adapt, optimize, polish and document playbooks guided the documented design and rendered review. |
| Frontend Design | Applied the installed guidance to compare three directions, choose a design and implement [DESIGN.md](../../DESIGN.md) in React and CSS. |
| Hallmark 2.0.0 | Ran the documented browser and preservation checks through the installed `scripts/sloplint.mjs`, with `--scope app --render --before` and `--scope app --render --kept`. The baseline source sweep reported 52 floor failures and 34 reflex findings. Playwright captured the baseline because the Hallmark renderer initially lacked Puppeteer. Subsequent Hallmark browser checks ran with an isolated, tooling-only Puppeteer installation. |
| Unslop | Reviewed spacing, color, icons, copy, type, layout, details, visuals and motion in order. Moved diagnostics behind disclosure, made the plan the focal decision, separated the source inspector and clarified empty, loading and error states. The workspace rail and actual incident data supply visual identity. |
| Vercel React Best Practices 1.0.0 | Reviewed effect dependencies, request cancellation, rendering, and bundle output. The stream-age timer runs only inside its open disclosure, so its ticks no longer rerender the entire workbench. The existing authentication scope, abort signals and request semantics remain authoritative. |
| Web Interface Guidelines | Applied the installed snapshot at revision `434b7f91364665f2f733b310ec54809bf8f37937`. Added workspace focus targets, explicit labels and field names, semantic tables, scrollable long records, and stronger disabled, error and loading states. |

## Verification scope

The foundation maps to P1, P2, P3, A1, A3, A4, B4, E1 and S1 in the [Omega ledger](../requirements-omega.json). A layout change supplies no Enterprise qualification evidence. The source changes replace the incumbent stylesheet and move existing registrations and views into the new structure. API endpoints, tabs with active streams, approval guards and backend implementation remain in place.

Playwright used the actual FastAPI handlers and a disposable simulator database. It exercised the built workbench, tenant denial, admission, recent selection, investigation, source inspection, queue refresh, experience empty results and the false-acknowledgment replay. Review keyboard runs confirmed approval, rejection, revocation and expiry, plus a real competing review whose stale attempt returned HTTP 409. Focus returned to the durable review result and expired controls were removed. The screen-reader spoken-output check remains deferred manual acceptance. A real external IdP login and authorized Enterprise redrive still require their existing qualification evidence.

Desktop, tablet and mobile captures use the same local fixture. The comparison archive includes the empty shell, incident, investigation, approval, evidence, history, operations, experience and replay. The additional error screenshot is an actual wrong-credential refusal from the local API.

| Check | Observed result |
| --- | --- |
| Hallmark rendered audit | Five workspace URLs, 25 browser renders, zero floor failures, zero reflex findings and zero warnings. The original five links and 32 handlers passed the preservation check. |
| Browser reflow | All five workspaces passed through seven viewport sizes from 320 to 1920 CSS px. The maximized native browser reported that the shell filled the document width. A populated evidence view had a single document scroll root and zero nested vertical scroll containers at 390 and 1920 px. |
| Automated accessibility | axe-core 4.11.1 reported zero violations for the tested WCAG checks in six states. Playwright verified focus, the review keyboard sequence, expiry cleanup and reduced motion. |
| Frontend tests | 20 Node tests passed, including three new checks for direct workspace addresses, retained incident anchors and unknown fragments. TypeScript and Vite built successfully. npm reported zero runtime vulnerabilities. |
| Backend regressions | 45 targeted API, authorization, release and ledger tests passed. Native disposable PostgreSQL and Temporal passed 24 applicable integration tests, and eight separate native Temporal recovery and replay tests passed. |
| Architecture | CodeGraph found no circular imports, Dependency Cruiser found zero violations, and Import Linter retained six contracts. Ruff correctness, tracked secrets and the migration graph passed. |
| Bundle | Baseline JS was 339.88 kB, 100.26 kB gzip. The redesigned build is 348.55 kB, 102.36 kB gzip. CSS is 14.51 kB, 3.96 kB gzip, and the self-hosted font adds 22.98 kB with its notice included in the distribution. |

The redesigned workspaces cover the browser UI already backed by the API. Live production integrations and unevidenced capabilities do not appear as usable console actions. The simulator fixture has no authorized enterprise security administrator, so browser redrive acceptance still needs that existing role and environment. Automated checks cover the examined states and do not establish complete WCAG conformance.
