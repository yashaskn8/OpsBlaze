"""G3 Docker-free native service deployment safeguards.

Static tests are not evidence of multiple independent production hosts.
"""

from pathlib import Path
from shutil import copytree

import pytest

from scripts.check_native_services import (
    REPOSITORY_ROOT, SERVICE_COMMANDS, check_native_services,
)


def test_all_native_service_roles_are_isolated_and_hardened():
    assert check_native_services() == []
    assert len(SERVICE_COMMANDS) == 6


@pytest.fixture
def deployed_copy(tmp_path: Path) -> Path:
    copytree(REPOSITORY_ROOT / "deploy", tmp_path / "deploy")
    return tmp_path


def alter(root: Path, filename: str, before: str, after: str) -> None:
    path = root / "deploy" / "systemd" / filename
    data = path.read_text(encoding="utf-8")
    assert before in data
    path.write_text(data.replace(before, after), encoding="utf-8")


@pytest.mark.parametrize(("name", "old", "new", "signal"), [
    ("opsblaze-projector.service", "User=opsblaze-projector", "User=opsblaze", "User"),
    ("opsblaze-projector.service", "EnvironmentFile=/etc/opsblaze/projector.env",
     "EnvironmentFile=/etc/opsblaze/opsblaze.env", "EnvironmentFile"),
    ("opsblaze-api.service", "--host 127.0.0.1", "--host 0.0.0.0", "ExecStart"),
    ("opsblaze-dispatcher.service", "NoNewPrivileges=true", "NoNewPrivileges=false",
     "NoNewPrivileges"),
    ("opsblaze-worker.service", "ProtectSystem=strict", "ProtectSystem=false",
     "ProtectSystem"),
    ("opsblaze-state-age.service", "Restart=always", "Restart=no", "restart"),
    ("opsblaze-api.service", "CapabilityBoundingSet=",
     "CapabilityBoundingSet=CAP_SYS_ADMIN", "CapabilityBoundingSet"),
])
def test_unsafe_service_mutations_fail_closed(
    deployed_copy, name, old, new, signal,
):
    alter(deployed_copy, name, old, new)
    problems = check_native_services(deployed_copy)
    assert any(signal in problem and name in problem for problem in problems)


def test_credential_literals_are_forbidden_in_projector_unit(deployed_copy):
    path = deployed_copy / "deploy/systemd/opsblaze-projector.service"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nEnvironment=OPSBLAZE_DATABASE_URL=private-password\n")
    assert any(
        "embedded" in issue for issue in check_native_services(deployed_copy)
    )


def test_missing_native_unit_is_rejected(deployed_copy):
    (deployed_copy / "deploy/systemd/opsblaze-worker.service").unlink()
    assert any("opsblaze-worker.service" in issue
               for issue in check_native_services(deployed_copy))


def test_retention_timer_cannot_silently_lose_catchup(deployed_copy):
    alter(
        deployed_copy, "opsblaze-maintenance.timer",
        "Persistent=true", "Persistent=false",
    )
    assert any("timer" in issue for issue in check_native_services(deployed_copy))


def test_no_external_effect_adapter_is_started_by_native_units():
    units = REPOSITORY_ROOT / "deploy/systemd"
    for filename, (_user, _env, command) in SERVICE_COMMANDS.items():
        content = (units / filename).read_text(encoding="utf-8")
        assert "docker" not in content.lower()
        assert "kubectl" not in content.lower()
        assert "sudo " not in content
        assert command in content
