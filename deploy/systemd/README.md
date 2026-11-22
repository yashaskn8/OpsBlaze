# OpsBlaze native systemd services (no Docker)

These units make the **simulator-only** API, Temporal dispatcher, Temporal
workflow worker, restricted experience projector, and aged-work monitor
manageable by systemd on separate Linux hosts. The existing bounded retention
timer remains available. CI validates the unit source, but does not deploy
to hosts or certify production recovery. G1–G6 stay BLOCKED.

## Host prerequisites and trust boundaries

- Native PostgreSQL and a persistent Temporal service, provisioned and
  backed up separately by authorized operators. Run Alembic migrations and
  `scripts/projector_grants.sql` with trusted database-owner credentials
  **before** starting the application services. Never run migrations using
  the projector account.
- Python 3.11+ environment installed under `/srv/opsblaze/.venv`, project
  code under `/srv/opsblaze`, accessible read-only to the two service users.
  Keep credentials out of the checkout and the virtual environment.
- Two unprivileged **distinct** Unix users/groups: `opsblaze` for the
  application roles and `opsblaze-projector` only for the local capsule
  projector. Neither should have sudo, container, or database owner rights.
- Root-controlled, non-world-readable environment files:
  `/etc/opsblaze/opsblaze.env` for the API/dispatcher/worker/monitor and
  `/etc/opsblaze/projector.env` for the restricted projector. Only systemd
  loads these; do not print their contents or place secrets in unit files.
- A separately managed HTTPS reverse proxy may forward to the API on
  **127.0.0.1:8000**. The API does not trust proxy headers by default.
  Configure OIDC/PKCE and production Temporal TLS independently.

For each **dispatcher and incident-worker host**, configure a distinct,
stable, non-sensitive `OPSBLAZE_WORKER_HOST_ID` in the main environment
file (for example `worker-a` and `worker-b`). Do not reuse an ID between
hosts or use an actual hostname containing sensitive inventory details.
The production readiness check requires two recently reported distinct IDs
for **both** the dispatcher and incident-worker roles. Its configurable
`OPSBLAZE_MIN_DISTINCT_WORKER_HOSTS` can require more (2–16), but cannot
be reduced to one. The host IDs are **self-reported by processes using
the common application database account**; a green check is operational
presence data only, never independent physical-host proof or a failover
acceptance certificate. Verify independently through host inventory,
network-level logs, separate machine credentials, and actual failure drills.

The two environment files deliberately contain **different** database
credentials. The main file supplies the application's own
`OPSBLAZE_DATABASE_URL` and a **passwordless role reference** such as
`postgresql+asyncpg://opsblaze_projector@db.internal:5432/opsblaze` for
`OPSBLAZE_PROJECTOR_DATABASE_URL`. The API checks its role/host identity
and requires a fresh projector heartbeat, **not** the projector password.

The **projector-only** file supplies the real restricted
`OPSBLAZE_PROJECTOR_DATABASE_URL` and a passwordless reference for the
primary application's `OPSBLAZE_DATABASE_URL` (same database/host but
username `opsblaze`). The projector verifies its authenticated PostgreSQL
login, checks restricted grants, and uses only its own DSN for database
operations and heartbeat. Its file must **not** contain the main
application's working password. Do not create a shared `.env` in the
checkout on these hosts.

Environment-specific OIDC, Temporal TLS, namespaces, certificate trust,
database TLS, backups, firewall rules, and independent secrets custody
still require actual operator setup. A checked-in unit is no such evidence.

## Run and verify on an authorized host

Review the configuration with the operator/security owner, provision the
two Unix accounts and environment files, and install only the required
units to `/etc/systemd/system/`. Prior to installation, validate the
tracked unit source without reading or loading any secrets:

```bash
python scripts/check_native_services.py
sudo systemd-analyze verify /etc/systemd/system/opsblaze-*.service
sudo systemctl daemon-reload
```

For an API/dispatcher/worker host, enable only the roles assigned to it:

```bash
sudo systemctl enable --now opsblaze-api.service
sudo systemctl enable --now opsblaze-dispatcher.service
sudo systemctl enable --now opsblaze-worker.service
sudo systemctl enable --now opsblaze-state-age.service
```

For an independently permissioned projector host:

```bash
sudo systemctl enable --now opsblaze-projector.service
```

Schedule bounded retention on one selected host (do not accidentally
multiply retention load):

```bash
sudo systemctl enable --now opsblaze-maintenance.timer
sudo systemctl list-timers opsblaze-maintenance.timer
```

Check status and logs using standard service tooling without printing
environment secrets. The production API readiness endpoint is
`GET http://127.0.0.1:8000/v1/health/ready`; it checks configured identity,
database, required worker heartbeats, and Temporal reachability, but
**does not** certify real-user identity flows, cross-host topology or
external adapters. A mere `healthz` is not a readiness check.

## Multi-host acceptance remains an external exercise

Install the dispatcher and Temporal workers on at least two genuinely
independent hosts, with separately durable PostgreSQL and Temporal
persistence, and verify inter-host failure recovery. The current
`worker_heartbeat` table records **roles**, not distinct physical hosts:
two green role heartbeats do not prove two-host operation. Keep mutation
paths disabled until the G3 team records timestamped fault injections,
actual RPO/RTO, verified PostgreSQL **and Temporal** restores, immutable
effect identities, rollback review, worker failover, load latency and
independently witnessed evidence.

All units enforce an unprivileged account, read-only system paths, disabled
ambient capabilities, a bounded restart policy and no privileged shell
startup hooks. The API binds only to loopback; no unit enables real
infrastructure effects or learned-policy activation.

See [production acceptance gates](../../docs/production-acceptance-gates.md).
