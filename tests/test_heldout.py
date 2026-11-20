"""E5 offline holdout runner tests; synthetic data cannot certify production."""

import json

import pytest

from backend.evaluation.heldout import (
    blinded_cases, canonical_digest, evaluate_heldout, read_json,
)


def fixture():
    cases = [
        {
            "case_id": f"holdout-{i:03d}",
            "family": "protected-unseen-family",
            "expected": "PROPOSE" if i < 32 else "ABSTAIN",
            "negative_control": i >= 32,
        }
        for i in range(64)
    ]
    oracle = {"cases": cases, "oracle_version": "synthetic-test-1"}
    manifest = {
        "version": 1,
        "oracle_sha256": canonical_digest(oracle),
        "min_pairs": 64,
        "training_families": ["training-only-family"],
    }
    runs = {
        "incumbent": [
            {
                "case_id": case["case_id"], "decision": "ABSTAIN",
                "probe_cost_units": 2, "latency_ms": 40,
            } for case in cases
        ],
        "candidate": [
            {
                "case_id": case["case_id"],
                "decision": case["expected"],
                "probe_cost_units": 1, "latency_ms": 20,
            } for case in cases
        ],
    }
    return manifest, oracle, runs


def test_synthetic_heldout_offline_pass_never_authorizes_policy():
    manifest, oracle, runs = fixture()
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "OFFLINE_PASS"
    assert report["metrics"]["pairs"] == 64
    assert report["metrics"]["negative_control_failures"] == 0
    assert report["metrics"]["conservative_95pct_interval"][0] > 0
    assert report["promotion_eligible"] is False
    assert report["verified_recovery_per_cost"] is None
    assert "expected" not in str(blinded_cases(oracle))
    assert "negative_control" not in str(blinded_cases(oracle))


def test_protected_oracle_digest_prevents_post_hoc_label_change():
    manifest, oracle, runs = fixture()
    oracle["cases"][0]["expected"] = "ABSTAIN"
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "INVALID"
    assert report["promotion_eligible"] is False


def test_training_family_in_holdout_is_rejected():
    manifest, oracle, runs = fixture()
    manifest["training_families"].append("protected-unseen-family")
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "INVALID"


def test_missing_trial_and_duplicate_trial_are_not_dropped_from_counts():
    manifest, oracle, runs = fixture()
    runs["candidate"].pop()
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "INVALID"
    manifest, oracle, runs = fixture()
    runs["candidate"].append(dict(runs["candidate"][0]))
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "INVALID"


def test_false_success_on_negative_control_fails():
    manifest, oracle, runs = fixture()
    runs["candidate"][-1]["decision"] = "PROPOSE"
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "OFFLINE_FAIL"
    assert report["metrics"]["candidate_unsafe"] == 1


def test_cost_or_latency_regression_fails():
    manifest, oracle, runs = fixture()
    for trial in runs["candidate"]:
        trial["probe_cost_units"] = 3
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "OFFLINE_FAIL"
    manifest, oracle, runs = fixture()
    for trial in runs["candidate"]:
        trial["latency_ms"] = 100
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "OFFLINE_FAIL"


def test_even_complete_small_sample_is_inconclusive_not_promotion():
    manifest, oracle, runs = fixture()
    for trial in runs["candidate"][:20]:
        trial["decision"] = "ABSTAIN"
    result = evaluate_heldout(manifest, oracle, runs)
    assert result["state"] == "INCONCLUSIVE"
    assert result["promotion_eligible"] is False


def test_oracle_input_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "case.json"
    path.write_text('{"cases":[],"cases":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        read_json(str(path))
