"""The offline replay archive must not accept corrupted or wrong-version inputs."""

import json
from uuid import uuid4

import pytest

from backend.workflows.replay_archive import CASES, load_archive, write_archive


def test_archive_roundtrip_requires_exact_source_and_detects_corruption(tmp_path):
    directory = tmp_path / "archive"
    source = "a" * 40
    histories = {case: (f"replay-{case}-{uuid4()}", '{"events": []}') for case in CASES}
    write_archive(directory, source, histories)
    assert load_archive(directory, source) == list(histories.values())
    with pytest.raises(ValueError, match="mismatched"):
        load_archive(directory, "b" * 40)
    (directory / "approve.json").write_text('{"events": [1]}')
    with pytest.raises(ValueError, match="digest mismatch"):
        load_archive(directory, source)


def test_archive_rejects_manifest_identity_and_missing_case(tmp_path):
    directory = tmp_path / "archive"
    source = "a" * 40
    histories = {case: (f"replay-{case}-{uuid4()}", '{}') for case in CASES}
    write_archive(directory, source, histories)
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["histories"]["approve"]["workflow_id"] = "unexpected"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity"):
        load_archive(directory, source)
    del manifest["histories"]["cancel"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="mismatched"):
        load_archive(directory, source)


def test_archive_rejects_symlink_and_oversized_history(tmp_path, monkeypatch):
    directory = tmp_path / "archive"
    source = "a" * 40
    histories = {case: (f"replay-{case}-{uuid4()}", '{}') for case in CASES}
    write_archive(directory, source, histories)
    path = directory / "approve.json"
    other = tmp_path / "other.json"
    other.write_text('{}')
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(ValueError, match="regular local file"):
        load_archive(directory, source)
    path.unlink()
    path.write_text('{}')
    monkeypatch.setattr("backend.workflows.replay_archive.MAX_HISTORY_BYTES", 1)
    with pytest.raises(ValueError, match="size"):
        load_archive(directory, source)
