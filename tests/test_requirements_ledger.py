"""Keep the feature ledger exhaustive at the capability level and evidence-aware."""
import json
import re
from collections import Counter, defaultdict
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
