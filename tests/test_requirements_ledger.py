"""Keep the feature ledger exhaustive at the capability level and evidence-aware."""
import json
import re
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path


def test_requirements_ledger_has_all_capabilities_and_valid_references():
    root = Path(__file__).resolve().parents[1]
    ledger = json.loads((root / "docs/requirements-ledger.json").read_text())
    rows = ledger["requirements"]
    expected = {
        *(f"B{i}" for i in range(1, 5)),
        *(f"A{i}" for i in range(1, 5)),
        *(f"P{i}" for i in range(1, 4)),
        "S1",
        *(f"E{i}" for i in range(1, 9)),
    }
    assert len(rows) == len(expected)
    assert {row["id"] for row in rows} == expected
    assert len({row["blueprint_section"] for row in rows}) == len(expected)
    for row in rows:
        assert set(row["dependencies"]) <= expected - {row["id"]}
        assert row["production_gates"]
        assert set(row["production_gates"]) <= {f"G{i}" for i in range(1, 7)}
        assert row["acceptance_criteria"] and row["remaining_validation"]
        assert row["evidence"]
        for evidence in row["evidence"]:
            assert re.fullmatch(r"[0-9a-f]{40}", evidence["commit"])
            assert (root / evidence["path"]).is_file()
            assert evidence["verification"]
    if any(row["production_status"] != "VERIFIED" for row in rows):
        assert ledger["production_acceptance"] == "BLOCKED"


def test_functionality_audit_has_bidirectional_traceability():
    root = Path(__file__).resolve().parents[1]
    ledger = json.loads((root / "docs/requirements-ledger.json").read_text(encoding="utf-8"))
    audit_path = root / ledger["checkpoint"]["functionality_audit"]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    capabilities = audit["capabilities"]
    expected = {entry["id"] for entry in ledger["requirements"]}
    assert len(capabilities) == len(expected) == 20
    assert {entry["capability"] for entry in capabilities} == expected
    assert audit["production_acceptance"] == ledger["production_acceptance"] == "BLOCKED"

    requirements = {}
    reverse = defaultdict(set)
    for capability in capabilities:
        assert capability["requirements"]
        first, last = capability["pdf_pages"]
        assert 1 <= first <= last <= audit["blueprint"]["pages"]
        for row in capability["requirements"]:
            key = row["id"]
            assert key.startswith(f"{capability['capability']}-") and key not in requirements
            assert row["requirement"] and row["evidence"]
            requirements[key] = row["status"]
            for path in set(row["implementation"] + row["tests"]):
                assert (root / path).is_file(), (key, path)
                reverse[path].add(key)

    assert dict(Counter(requirements.values())) == audit["status_counts"]
    assert {path: set(ids) for path, ids in audit["reverse_index"].items()} == reverse
    for item in audit["findings"] + audit["repair_backlog"]:
        assert set(item["requirements"]) <= requirements.keys()


def test_dual_profile_report_preserves_the_blueprint_and_derives_release_progress():
    from scripts.release_requirements import release_requirements

    root = Path(__file__).resolve().parents[1]
    commit = "b" * 40
    report = release_requirements(root / "docs", source_commit=commit)
    assert report["source_commit"] == commit
    assert re.fullmatch(r"[0-9a-f]{40}", report["evidence_baseline"])
    rows = report["requirements"]
    assert len({row["id"] for row in rows}) == len(rows) == 176
    assert len({row["blueprint_section"] for row in rows}) == 20
    assert report["historical_status_counts"] == {
        "VERIFIED WORKING": 154, "PARTIALLY IMPLEMENTED": 2, "EXTERNALLY BLOCKED": 20,
    }
    assert report["community"]["release_status"] == "PENDING"
    assert report["enterprise"]["release_status"] == "ENTERPRISE_BLOCKED"
    assert set(report["community"]["gates"]) == {f"C{i}" for i in range(1, 7)}
    assert all(gate["status"] == "PENDING" for gate in report["community"]["gates"].values())
    assert report["community"]["accepted"] + report["community"]["pending"] == report["community"]["applicable"]
    assert report["community"]["applicable"] + report["community"]["not_applicable"] == len(rows)
    assert report["enterprise"]["blocked"] == len(rows)

    by_id = {row["id"]: row for row in rows}
    assert by_id["P2-08"]["community"]["applicable"] is True
    assert by_id["P2-08"]["community"]["status"] == "PARTIALLY_IMPLEMENTED"
    assert by_id["B1-08"]["community"]["applicable"] is True
    assert by_id["B1-08"]["runtime_verification"] == "PARTIALLY_IMPLEMENTED"
    assert by_id["B2-07"]["runtime_verification"] == "NOT_APPLICABLE"
    assert by_id["B2-07"]["community"]["status"] == "NOT_APPLICABLE_TO_COMMUNITY"
    assert by_id["B2-07"]["enterprise"]["status"] == "ENTERPRISE_BLOCKED"
    for row in rows:
        assert row["implementation"] and row["consumers"] and row["evidence"]
        assert row["tests"] or row["remaining"]
        assert row["engineering_status"] and row["runtime_verification"]


def test_omega_report_preserves_atomic_scope_and_excludes_only_license_creation():
    from scripts.release_requirements import release_requirements

    report = release_requirements(Path(__file__).resolve().parents[1] / "docs")
    omega = report["omega"]
    rows = {row["id"]: row for row in omega["requirements"]}
    assert len(rows) == 269
    assert omega["origins"] == {"ORIGINAL": 193, "PROJECT_SPECIFIC": 5, "SHARED": 46, "AI": 25}
    assert set(omega["original_anchors"]) == {row["id"] for row in report["requirements"]}
    assert len({child for children in omega["original_anchors"].values() for child in children}) == 193
    assert omega["community"]["applicability"] == {
        "MANDATORY": 224, "CONDITIONAL": 13, "RECOMMENDED": 8, "NOT_APPLICABLE": 23,
    }
    assert omega["community"]["accepted"] == omega["enterprise"]["accepted"] == 0
    assert omega["exclusions"] == {"SH-REL-01": "USER_AUTHORIZED_SCOPE_EXCLUSION"}
    assert rows["SH-REL-01"]["profile_status"]["OpsBlaze"] == "NOT_IMPLEMENTED"
    assert "AI-GRD-03" not in rows
    assert rows["OB-B1-08a"]["verification"]["community"] == "PARTIALLY_IMPLEMENTED"
    assert rows["OB-B1-08a"]["profile_dependencies"] == {"community": [], "enterprise": ["G3"]}
    assert rows["OB-B1-08b"]["verification"] == {
        "community": "NOT_APPLICABLE", "enterprise": "BLOCKED_EXTERNAL_DEPENDENCY",
    }
    assert rows["SH-SEC-07"]["verification"]["community"] == "NOT_APPLICABLE"
    assert rows["AI-EVAL-07"]["verification"]["community"] == "NOT_APPLICABLE"
    assert rows["OB-P2-08a"]["verification"]["community"] == "ACCEPTANCE_PENDING"


def test_release_decision_requires_commit_and_hashes_the_built_artifact(tmp_path):
    root = Path(__file__).resolve().parents[1]
    artifact = tmp_path / "candidate.tar.gz"
    commit = "a" * 40
    marker = tmp_path / "SOURCE_COMMIT"
    marker.write_bytes((commit + "\n").encode("ascii"))
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(marker, arcname="./SOURCE_COMMIT")
    command = [sys.executable, "scripts/release_requirements.py", "--decision"]
    for args in (
        ["--source-commit", commit],
        ["--artifact", str(artifact)],
        ["--source-commit", commit, "--artifact", str(tmp_path / "missing.tar.gz")],
        ["--source-commit", "b" * 40, "--artifact", str(artifact)],
    ):
        result = subprocess.run([*command, *args], cwd=root, capture_output=True, text=True)
        assert result.returncode == 2 and not result.stdout

    result = subprocess.run(
        [*command, "--source-commit", commit, "--artifact", str(artifact)],
        cwd=root, capture_output=True, text=True, check=True,
    )
    decision = json.loads(result.stdout)
    assert decision["report_kind"] == "release_decision"
    assert decision["source_commit"] == commit
    assert decision["artifact_sha256"] == sha256(artifact.read_bytes()).hexdigest()
    assert decision["community"]["release_status"] == "PENDING"
    assert decision["enterprise"]["release_status"] == "ENTERPRISE_BLOCKED"
