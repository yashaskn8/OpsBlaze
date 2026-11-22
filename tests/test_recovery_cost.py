"""G4 paired recovery-cost evidence: no measured input authorizes promotion."""

import json
import sys
from copy import deepcopy

import pytest

from backend.evaluation.heldout import canonical_digest, evaluate_heldout, main
from backend.evaluation.recovery_cost import measured_recovery_cost


def recovery_fixture():
    cases = [
        {
            "case_id": f"sealed-{i:03d}",
            "family": "family-a" if i < 32 else "family-b",
            "expected": "PROPOSE" if i < 48 else "ABSTAIN",
            "negative_control": i >= 48,
        }
        for i in range(64)
    ]
    oracle = {"cases": cases, "oracle_version": "synthetic-recovery-fixture"}
    runs = {
        "candidate": [{
            "case_id": case["case_id"], "decision": case["expected"],
            "probe_cost_units": 1, "latency_ms": 20,
        } for case in cases],
        "incumbent": [{
            "case_id": case["case_id"], "decision": "ABSTAIN",
            "probe_cost_units": 2, "latency_ms": 40,
        } for case in cases],
    }
    def make_row(i, arm):
        if arm == "candidate":
            result = (
                "VERIFIED" if i < 30 else "DISPUTED" if i < 40
                else "UNKNOWN" if i < 52 else "FAILED"
            )
        else:
            result = (
                "VERIFIED" if i < 20 else "UNKNOWN" if i < 30
                else "DISPUTED" if i < 40 else "FAILED"
            )
        return {
            "case_id": cases[i]["case_id"],
            "outcome": result,
            "witness_sha256": "a" * 64 if result == "VERIFIED" else None,
            "probe_microcredits": 3 if arm == "incumbent" else 2,
            "model_microcredits": 1,
            "provider_microcredits": 2,
            "status_microcredits": 1,
            "compute_microcredits": 1,
            "latency_ms": 20 if arm == "candidate" else 40,
        }
    observations = {
        "schema": "opsblaze-e5-recovery-cost-v1",
        "oracle_sha256": canonical_digest(oracle),
        "trials_sha256": canonical_digest(runs),
        "arms": {
            arm: [make_row(i, arm) for i in range(64)]
            for arm in ("candidate", "incumbent")
        },
    }
    manifest = {
        "version": 1, "oracle_sha256": canonical_digest(oracle),
        "min_pairs": 64, "training_families": ["training"],
    }
    return manifest, oracle, runs, observations


def test_counts_unknown_disputed_and_every_cost_component():
    manifest, oracle, runs, observations = recovery_fixture()
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "OFFLINE_PASS"
    result = measured_recovery_cost(oracle, runs, observations)
    assert result["pairs"] == 64
    assert result["arms"]["candidate"]["verified"] == 30
    assert result["arms"]["candidate"]["total_microcredits"] == 448
    assert result["arms"]["incumbent"]["total_microcredits"] == 512
    assert result["arms"]["candidate"]["outcomes"] == {
        "DISPUTED": 10, "FAILED": 12, "UNKNOWN": 12, "VERIFIED": 30,
    }
    assert result["candidate_ratio_higher"] is True
    assert result["promotion_eligible"] is False
    assert result["external_witness_independence_certified"] is False
    assert "rows" not in result["arms"]["candidate"]
    assert "case_id" not in str(result)


@pytest.mark.parametrize("change", [
    "wrong_oracle", "wrong_trials", "missing_case", "duplicate_case",
    "extra_case", "boolean_cost", "negative_cost", "missing_cost",
    "invalid_status", "missing_witness", "forged_unverified_witness",
    "wrong_field", "zero_total_cost", "missing_arm", "wrong_schema",
    "float_latency",
])
def test_measurement_rejects_invalid_or_unbound_claims(change):
    _manifest, oracle, runs, observations = recovery_fixture()
    observations = deepcopy(observations)
    candidate = observations["arms"]["candidate"]
    if change == "wrong_oracle":
        observations["oracle_sha256"] = "0" * 64
    elif change == "wrong_trials":
        observations["trials_sha256"] = "0" * 64
    elif change == "missing_case":
        candidate.pop()
    elif change == "duplicate_case":
        candidate[0] = deepcopy(candidate[1])
    elif change == "extra_case":
        candidate.append({**candidate[0], "case_id": "extra"})
    elif change == "boolean_cost":
        candidate[0]["probe_microcredits"] = True
    elif change == "negative_cost":
        candidate[0]["model_microcredits"] = -1
    elif change == "missing_cost":
        candidate[0].pop("compute_microcredits")
    elif change == "invalid_status":
        candidate[0]["outcome"] = "RECOVERED_UNWITNESSED"
    elif change == "missing_witness":
        candidate[0]["witness_sha256"] = None
    elif change == "forged_unverified_witness":
        candidate[50]["witness_sha256"] = "f" * 64
    elif change == "wrong_field":
        candidate[0]["expected"] = "PROPOSE"
    elif change == "zero_total_cost":
        for row in candidate:
            for field in (
                "probe_microcredits", "model_microcredits",
                "provider_microcredits", "status_microcredits", "compute_microcredits",
            ):
                row[field] = 0
    elif change == "missing_arm":
        observations["arms"].pop("incumbent")
    elif change == "wrong_schema":
        observations["schema"] = "v0"
    else:
        candidate[0]["latency_ms"] = 1.5
    with pytest.raises(ValueError):
        measured_recovery_cost(oracle, runs, observations)


def test_family_regression_cannot_hide_behind_aggregate_ratio():
    _manifest, oracle, runs, observations = recovery_fixture()
    # Candidate wins on aggregate but recovers fewer of family-b's cases.
    for i in range(32, 42):
        observations["arms"]["incumbent"][i]["outcome"] = "VERIFIED"
        observations["arms"]["incumbent"][i]["witness_sha256"] = "f" * 64
    report = measured_recovery_cost(oracle, runs, observations)
    assert report["candidate_ratio_higher"] is True
    assert report["regressed_families"] == ["family-b"]
    assert report["promotion_eligible"] is False


def test_cli_accepts_audited_measurements_but_never_certifies_them(
    tmp_path, monkeypatch, capsys,
):
    manifest, oracle, runs, observations = recovery_fixture()
    for name, obj in (
        ("manifest", manifest), ("oracle", oracle), ("trials", runs),
        ("observations", observations),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")
    args = [
        "heldout", "--manifest", str(tmp_path / "manifest.json"),
        "--oracle", str(tmp_path / "oracle.json"),
        "--trials", str(tmp_path / "trials.json"),
        "--recovery-measurements", str(tmp_path / "observations.json"),
    ]
    monkeypatch.setattr(sys, "argv", args)
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "OFFLINE_PASS"
    assert output["recovery_cost"]["pairs"] == 64
    assert output["verified_recovery_per_cost"]["candidate"] > 0
    assert output["preregistration"]["status"] == "UNSIGNED_LAB_ONLY"
    assert output["promotion_eligible"] is False

    observations["trials_sha256"] = "f" * 64
    (tmp_path / "observations.json").write_text(
        json.dumps(observations), encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    denied = json.loads(capsys.readouterr().out)
    assert denied["state"] == "INVALID"
    assert denied["promotion_eligible"] is False


def test_negative_control_cannot_count_as_verified_recovery():
    _manifest, oracle, runs, observations = recovery_fixture()
    row = observations["arms"]["candidate"][60]
    row["outcome"] = "VERIFIED"
    row["witness_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="negative safety control"):
        measured_recovery_cost(oracle, runs, observations)
