import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api import app
from backend.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]


def test_default_runtime_and_release_bundle_have_the_same_restricted_profile(monkeypatch):
    monkeypatch.delenv("OPSBLAZE_RELEASE_PROFILE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.release_profile == "community"
    commit = "a" * 40
    bundled = subprocess.run(
        [sys.executable, "-m", "backend.release_profile", commit], cwd=ROOT,
        capture_output=True, check=True, text=True,
    )
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/release-profile")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    profile = response.json()
    assert json.loads(bundled.stdout) == {**profile, "source_commit": commit}
    assert profile == {
        "architecture_version": "2.0",
        "profile": "community",
        "effect_scope": "simulator_only",
        "live_policy_promotion": False,
        "excluded_capabilities": [
            "external_identity_qualification",
            "external_adapter_execution",
            "multi_host_availability_qualification",
            "independent_ai_qualification",
            "live_policy_promotion",
            "independent_release_authority",
            "external_security_and_privacy_qualification",
        ],
        "enterprise_acceptance": "ENTERPRISE_BLOCKED",
    }


@pytest.mark.parametrize("value", ["enterprise", "Enterprise", "unknown"])
def test_unsupported_profile_blocks_api_startup_even_with_production_settings(monkeypatch, value):
    monkeypatch.setenv("OPSBLAZE_RELEASE_PROFILE", value)
    monkeypatch.setenv("OPSBLAZE_ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="release_profile"), TestClient(app):
            pytest.fail("unsupported profile started")
    finally:
        get_settings.cache_clear()


def test_native_launcher_serves_the_ui_and_existing_api_on_loopback(tmp_path, monkeypatch):
    import uvicorn

    from backend.community import main

    (tmp_path / "index.html").write_text("<!doctype html><title>OpsBlaze</title>", encoding="utf-8")
    monkeypatch.setattr(app.router, "routes", app.router.routes.copy())
    monkeypatch.setattr(sys, "argv", ["community", "--frontend-dir", str(tmp_path), "--port", "8147"])

    def verify_server(application, *, host, port, proxy_headers):
        assert host == "127.0.0.1" and port == 8147 and proxy_headers is False
        with TestClient(application) as client:
            assert "<title>OpsBlaze</title>" in client.get("/").text
            assert client.get("/release-profile").json()["profile"] == "community"
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/v1/incidents").status_code in {401, 403, 422, 503}

    monkeypatch.setattr(uvicorn, "run", verify_server)
    main()


@pytest.mark.parametrize(("args", "error"), [
    (["--port", "0"], "port must be between"),
    (["--host", "0.0.0.0"], "unrecognized arguments"),
    ([], "missing index.html"),
])
def test_native_launcher_rejects_unsafe_or_incomplete_invocations(tmp_path, monkeypatch, capsys, args, error):
    from backend.community import main

    monkeypatch.setattr(sys, "argv", ["community", "--frontend-dir", str(tmp_path), *args])
    with pytest.raises(SystemExit) as failed:
        main()
    assert failed.value.code == 2
    assert error in capsys.readouterr().err


@pytest.mark.parametrize(("name", "value", "refusal"), [
    ("OPSBLAZE_EFFECT_ADAPTER", "external", "effect_adapter"),
    ("OPSBLAZE_LIVE_POLICY_PROMOTION", "true", "live policy promotion"),
])
def test_community_process_refuses_unsupported_runtime_scope(tmp_path, monkeypatch, name, value, refusal):
    (tmp_path / "index.html").write_text("<title>OpsBlaze</title>", encoding="utf-8")
    monkeypatch.setenv("OPSBLAZE_RELEASE_PROFILE", "community")
    monkeypatch.setenv(name, value)
    result = subprocess.run(
        [sys.executable, "-m", "backend.community", "--frontend-dir", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 2
    assert refusal in result.stderr
    assert "Application startup complete" not in result.stderr


def test_local_credentials_are_issued_privately_and_rotate_after_restart(tmp_path, monkeypatch):
    import os
    import stat

    paths = [tmp_path / "v1.credentials.env", tmp_path / "v2.credentials.env"]
    for epoch, path in enumerate(paths, 1):
        result = subprocess.run(
            [sys.executable, "scripts/issue_local_credentials.py", str(path), "--review-epoch", str(epoch)],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        assert not result.stdout
        if os.name == "nt":
            monkeypatch.setenv("OPSBLAZE_TEST_CREDENTIAL_PATH", str(path))
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", (
                    "$file = [System.IO.FileInfo]::new($env:OPSBLAZE_TEST_CREDENTIAL_PATH); "
                    "$acl = $file.GetAccessControl(); "
                    "$sid = [System.Security.Principal.SecurityIdentifier]; "
                    "$rules = $acl.GetAccessRules($true, $true, $sid); "
                    "if (!$acl.AreAccessRulesProtected -or $rules.Count -ne 1 "
                    "-or $rules[0].IdentityReference -ne $acl.GetOwner($sid)) { exit 1 }"
                )],
                capture_output=True, check=True,
            )
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    create_again = subprocess.run(
        [sys.executable, "scripts/issue_local_credentials.py", str(paths[0])],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert create_again.returncode == 2 and not create_again.stdout

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPSBLAZE_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("OPSBLAZE_REVIEW_TOKEN", raising=False)
    monkeypatch.delenv("OPSBLAZE_REVIEW_EPOCH", raising=False)
    monkeypatch.setenv("OPSBLAZE_ENVIRONMENT", "development")
    get_settings.cache_clear()
    try:
        def verify_credential(token, status):
            with TestClient(app) as client:
                response = client.get("/v1/auth/me", headers={
                    "X-Tenant-Id": "demo", "X-OpsBlaze-Token": token,
                })
            assert response.status_code == status

        monkeypatch.setenv("OPSBLAZE_CREDENTIALS_FILE", str(paths[0]))
        original = get_settings()
        issued_distinct_credentials = (
            len(original.local_token) == len(original.review_token) == 43
            and original.local_token != original.review_token
        )
        assert issued_distinct_credentials
        verify_credential(original.local_token, 200)
        verify_credential(original.review_token, 401)

        monkeypatch.setenv("OPSBLAZE_CREDENTIALS_FILE", str(paths[1]))
        get_settings.cache_clear()
        rotated = get_settings()
        reviewer_rotated = rotated.review_token != original.review_token
        assert rotated.review_epoch == 2 and reviewer_rotated
        verify_credential(original.local_token, 401)
        verify_credential(rotated.local_token, 200)
    finally:
        get_settings.cache_clear()


def test_sample_credentials_from_old_env_templates_are_invalid():
    settings = Settings(
        _env_file=None,
        local_token="replace-with-a-long-random-local-token",
        review_token="use-a-different-long-random-reviewer-token",
    )
    assert settings.local_token is None and settings.review_token is None
