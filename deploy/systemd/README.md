# OpsBlaze native systemd maintenance (no Docker)

The project contains one minimal systemd timer for the existing bounded
PostgreSQL retention jobs. Installing these templates is an **operator
action** on a Linux host; CI verifies source behavior but does not deploy
them.

Assumptions to change for your host: an unprivileged `opsblaze` Unix
user/group, a checked-out application under `/srv/opsblaze`, a Python
venv under `/srv/opsblaze/.venv`, and a root-controlled
`/etc/opsblaze/opsblaze.env` with database settings. Never put secrets
inside these unit files or commit the environment file.

After running migrations, an authorized host administrator can install
both unit files to `/etc/systemd/system/`, then use:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now opsblaze-maintenance.timer
sudo systemctl list-timers opsblaze-maintenance.timer
sudo systemctl start opsblaze-maintenance.service
sudo journalctl -u opsblaze-maintenance.service --no-pager -n 50
```

The timer runs hourly, with a small randomized delay and a persistent
missed-run catchup after host restart. Every invocation processes at
most 20 batches of 500 rows per category, then terminates. Incomplete
backlogs continue on the next run. Errors exit nonzero and appear in
the journal; a failed run does not erase durable evidence.

This is **retention scheduling only**. Production-grade service
management, TLS reverse proxy, secret lifecycle, backups, restore drills,
load tests and real IdP/Temporal integration remain separate release
requirements.
