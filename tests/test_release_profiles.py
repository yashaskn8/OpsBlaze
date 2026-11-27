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
