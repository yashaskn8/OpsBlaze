# OpsBlaze engineering rules

Follow the TITAN-20 blueprint, with the smallest verifiable implementation first.

- Work directly on main. No branches, pull requests, or Docker.
- Prefer short, explicit Python and TypeScript over frameworks and abstractions.
- Never claim a feature is complete until its failure modes are tested.
- PostgreSQL owns incident admission, approvals, and effects. Temporal will own durable workflow execution.
- Commit incident and outbox intent atomically. Never call external systems inside a database transaction.
- Keep tenant data isolated. The development token is NOT production authentication.
- Agents cannot grant themselves permissions or run unapproved mutations.
- Add tests for changed behavior. Run them before claiming verification.
- Never commit credentials, local .env files, or raw incident evidence.
- Read CONSTRAINTS.md before writing code. Do not weaken it to make a change pass.
