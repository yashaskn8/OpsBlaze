"""Static safety checks for Docker-free, native OpsBlaze systemd units.

This checks deployable source files, not a running host or production gate.
It deliberately never reads /etc/opsblaze secrets or invokes systemctl.
"""

from configparser import ConfigParser, Error as ConfigError
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UNIT_DIRECTORY = Path("deploy/systemd")

SERVICE_COMMANDS = {
    "opsblaze-api.service": (
        "opsblaze",
        "opsblaze.env",
        (
            "/srv/opsblaze/.venv/bin/uvicorn backend.api:app "
            "--host 127.0.0.1 --port 8000 --no-proxy-headers"
        ),
    ),
    "opsblaze-dispatcher.service": (
        "opsblaze",
        "opsblaze.env",
        "/srv/opsblaze/.venv/bin/python -m backend.workers.dispatcher",
    ),
    "opsblaze-worker.service": (
        "opsblaze",
        "opsblaze.env",
        "/srv/opsblaze/.venv/bin/python -m backend.workers.incident_worker",
    ),
    "opsblaze-projector.service": (
        "opsblaze-projector",
        "projector.env",
        "/srv/opsblaze/.venv/bin/python -m backend.workers.experience_projector",
    ),
    "opsblaze-state-age.service": (
        "opsblaze",
        "opsblaze.env",
        "/srv/opsblaze/.venv/bin/python -m backend.workers.state_age",
    ),
    "opsblaze-maintenance.service": (
        "opsblaze",
        "opsblaze.env",
        "/srv/opsblaze/.venv/bin/python -m backend.workers.maintenance",
    ),
    "opsblaze-temporal-community.service": (
        "opsblaze-temporal",
        None,
        (
            "/usr/local/bin/temporal server start-dev --ip 127.0.0.1 --port 7233 --headless "
            "--db-filename /var/lib/opsblaze-temporal/temporal.db"
        ),
    ),
}

REQUIRED_HARDENING = {
    "NoNewPrivileges": "true",
    "PrivateTmp": "true",
    "ProtectSystem": "strict",
    "ProtectHome": "true",
    "ProtectKernelTunables": "true",
    "ProtectControlGroups": "true",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
}


def read_unit(path: Path) -> tuple[ConfigParser, str]:
    source = path.read_text(encoding="utf-8")
    config = ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string(source)
    return config, source


def check_native_services(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Return invariant violations; never read the real host's secret files."""
    problems: list[str] = []
    directory = root / UNIT_DIRECTORY
    for filename, (identity, envfile, command) in SERVICE_COMMANDS.items():
        path = directory / filename
        try:
            config, source = read_unit(path)
        except (OSError, UnicodeError, ConfigError) as exc:
            problems.append(f"{filename}: missing/invalid unit ({type(exc).__name__})")
            continue
        if "Service" not in config or "Unit" not in config:
            problems.append(f"{filename}: missing systemd Unit/Service section")
            continue
        service = config["Service"]
        requirements = {
            **REQUIRED_HARDENING,
            "User": identity,
            "Group": identity,
            "ExecStart": command,
        }
        if envfile is None:
            requirements.update({
                "WorkingDirectory": "/var/lib/opsblaze-temporal",
                "StateDirectory": "opsblaze-temporal",
                "StateDirectoryMode": "0700",
                "UMask": "0077",
            })
            if "EnvironmentFile" in service or "Environment" in service:
                problems.append(f"{filename}: Community Temporal must not load application credentials")
        else:
            requirements.update({
                "WorkingDirectory": "/srv/opsblaze",
                "EnvironmentFile": f"/etc/opsblaze/{envfile}",
            })
            if source.count("Environment=PYTHONDONTWRITEBYTECODE=1") != 1:
                problems.append(f"{filename}: Python bytecode must stay disabled")
            if source.count("Environment=PYTHONUNBUFFERED=1") != 1 and filename != "opsblaze-maintenance.service":
                problems.append(f"{filename}: Python logs must be unbuffered")
        for name, expected in requirements.items():
            if service.get(name) != expected:
                problems.append(f"{filename}: unsafe/missing {name}")
        for forbidden in ("ExecStartPre", "ExecStartPost", "ExecStopPost", "ExecReload"):
            if forbidden in service:
                problems.append(f"{filename}: unexpected {forbidden} hook")
        if filename != "opsblaze-maintenance.service":
            if service.get("Restart") != "always":
                problems.append(f"{filename}: restart policy must recover workers")
            if config.get("Install", "WantedBy", fallback="") != "multi-user.target":
                problems.append(f"{filename}: missing managed-service install target")
        if filename == "opsblaze-projector.service":
            if "OPSBLAZE_DATABASE_URL=" in source or "OPSBLAZE_PROJECTOR_DATABASE_URL=" in source:
                problems.append(f"{filename}: database credentials must not be embedded")
    timer = directory / "opsblaze-maintenance.timer"
    try:
        config, _ = read_unit(timer)
        if (
            config.get("Timer", "Persistent", fallback="") != "true"
            or config.get("Timer", "Unit", fallback="") != "opsblaze-maintenance.service"
        ):
            problems.append("opsblaze-maintenance.timer: required bounded retention timer missing")
    except (OSError, UnicodeError, ConfigError):
        problems.append("opsblaze-maintenance.timer: unreadable")
    return problems


def main() -> None:
    failures = check_native_services()
    if failures:
        for failure in failures:
            print(f"INVALID: {failure}")
        raise SystemExit(1)
    print("Native service unit source checks PASS (not a host deployment certificate)")


if __name__ == "__main__":
    main()
