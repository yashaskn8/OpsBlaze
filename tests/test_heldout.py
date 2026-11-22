"""E5 offline holdout runner tests; synthetic data cannot certify production."""

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


@pytest.mark.parametrize("mutation", ["almost_no_controls", "bad_training_family", "boolean_version"])
def test_redteam_heldout_metadata_tampering_is_rejected(mutation):
    manifest, oracle, runs = fixture()
    if mutation == "almost_no_controls":
        for case in oracle["cases"][32:-1]:
            case["negative_control"] = False
        manifest["oracle_sha256"] = canonical_digest(oracle)
    elif mutation == "bad_training_family":
        manifest["training_families"] = [42]
    else:
        manifest["version"] = True
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "INVALID"
    assert report["promotion_eligible"] is False


def test_redteam_trial_mutation_changes_report_digest_without_leaking_labels():
    manifest, oracle, runs = fixture()
    original = evaluate_heldout(manifest, oracle, runs)
    runs["candidate"][0]["probe_cost_units"] += 1
    altered = evaluate_heldout(manifest, oracle, runs)
    assert original["trials_digest"] != altered["trials_digest"]
    assert original["oracle_digest"] == altered["oracle_digest"]
    assert "cases" not in original
    assert original["promotion_eligible"] is False


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_redteam_evaluation_json_rejects_nonstandard_numbers(tmp_path, literal):
    path = tmp_path / "untrusted-evaluation.json"
    path.write_text('{"score":' + literal + '}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-standard numeric"):
        read_json(str(path))


def test_redteam_evaluation_digest_never_accepts_nonfinite_values():
    manifest, oracle, runs = fixture()
    runs["candidate"][0]["optional_metric"] = float("nan")
    assert evaluate_heldout(manifest, oracle, runs)["state"] == "INVALID"


def test_aggregate_gain_cannot_hide_a_family_regression():
    # Before per-family gating this was an OFFLINE_PASS: aggregate delta
    # exceeds its conservative confidence margin despite a harmed family.
    cases = [
        {
            "case_id": f"paired-{i:03d}",
            "family": "family-a" if i < 64 else "family-b",
            "expected": "PROPOSE" if i < 74 else "ABSTAIN",
            "negative_control": i >= 74,
        }
        for i in range(128)
    ]
    oracle = {"cases": cases, "oracle_version": "synthetic-family-regression"}
    manifest = {
        "version": 1, "oracle_sha256": canonical_digest(oracle),
        "min_pairs": 128, "training_families": ["unseen-training-family"],
    }
    runs = {
        "candidate": [
            {
                "case_id": case["case_id"],
                "decision": "PROPOSE" if i < 64 else "ABSTAIN",
                "probe_cost_units": 1, "latency_ms": 20,
            }
            for i, case in enumerate(cases)
        ],
        "incumbent": [
            {
                "case_id": case["case_id"],
                "decision": "PROPOSE" if 64 <= i < 74 else "ABSTAIN",
                "probe_cost_units": 2, "latency_ms": 40,
            }
            for i, case in enumerate(cases)
        ],
    }
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["metrics"]["conservative_95pct_interval"][0] > 0
    assert report["metrics"]["candidate_unsafe"] == 0
    assert report["metrics"]["regressed_families"] == ["family-b"]
    assert report["metrics"]["family_metrics"]["family-b"]["candidate_correct"] == 54
    assert report["metrics"]["family_metrics"]["family-b"]["incumbent_correct"] == 64
    assert report["state"] == "OFFLINE_FAIL"
    assert report["promotion_eligible"] is False


def test_small_holdout_family_does_not_expose_case_level_results():
    manifest, oracle, runs = fixture()
    oracle["cases"][-1]["family"] = "single-case-family"
    manifest["oracle_sha256"] = canonical_digest(oracle)
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "INVALID"
    assert report["reason"] == "insufficient cases in a holdout family"
    assert "metrics" not in report


@pytest.mark.parametrize("field,value", [
    ("expected", "PROPOSE"),
    ("negative_control", False),
    ("oracle_label", "ABSTAIN"),
    ("unregistered_note", "not part of the frozen trial schema"),
])
def test_unblinded_or_unregistered_trial_metadata_is_rejected(field, value):
    manifest, oracle, runs = fixture()
    runs["candidate"][0][field] = value
    report = evaluate_heldout(manifest, oracle, runs)
    assert report["state"] == "INVALID"
    assert report["reason"] == "unblinded or unexpected trial field"
    assert report["promotion_eligible"] is False
    assert "metrics" not in report


def test_evaluation_file_read_is_bounded_by_actual_bytes(tmp_path):
    from backend.evaluation.heldout import MAX_BYTES
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="fixed size bound"):
        read_json(str(oversized))


def test_evaluation_file_rejects_invalid_utf8(tmp_path):
    bad_encoding = tmp_path / "bad-encoding.json"
    bad_encoding.write_bytes(b'{"manifest": "' + bytes([0xFF]) + b'"}')
    with pytest.raises(ValueError, match="UTF-8"):
        read_json(str(bad_encoding))


def test_family_cost_regression_fails_even_if_total_cost_improves():
    manifest, oracle, runs = fixture()
    for i, case in enumerate(oracle["cases"]):
        case["family"] = "family-a" if i < 32 else "family-b"
        runs["candidate"][i]["probe_cost_units"] = 0 if i < 32 else 3
    manifest["oracle_sha256"] = canonical_digest(oracle)
    report = evaluate_heldout(manifest, oracle, runs)
    metrics = report["metrics"]
    assert metrics["candidate_probe_cost_units"] < metrics["incumbent_probe_cost_units"]
    assert metrics["conservative_95pct_interval"][0] > 0
    assert metrics["regressed_families"] == []
    assert metrics["resource_regressed_families"] == ["family-b"]
    assert report["state"] == "OFFLINE_FAIL"


def test_minor_family_latency_regression_is_not_masked_by_global_p95():
    # Ten slow cases are less than 5% of the sample, so the global p95
    # remains fast even though this entire family is slower.
    cases = [
        {
            "case_id": f"latency-{i:03d}",
            "family": "small-family" if i >= 246 else "large-family",
            "expected": "PROPOSE" if i < 200 else "ABSTAIN",
            "negative_control": i >= 200,
        }
        for i in range(256)
    ]
    oracle = {"cases": cases, "oracle_version": "synthetic-latency"}
    manifest = {
        "version": 1, "oracle_sha256": canonical_digest(oracle),
        "min_pairs": 256, "training_families": [],
    }
    runs = {
        "candidate": [
            {
                "case_id": case["case_id"],
                "decision": case["expected"],
                "probe_cost_units": 1,
                "latency_ms": 100 if i >= 246 else 20,
            }
            for i, case in enumerate(cases)
        ],
        "incumbent": [
            {
                "case_id": case["case_id"],
                "decision": "ABSTAIN",
                "probe_cost_units": 2, "latency_ms": 40,
            }
            for case in cases
        ],
    }
    report = evaluate_heldout(manifest, oracle, runs)
    metrics = report["metrics"]
    assert metrics["candidate_p95_latency_ms"] < metrics["incumbent_p95_latency_ms"]
    assert metrics["resource_regressed_families"] == ["small-family"]
    assert report["state"] == "OFFLINE_FAIL"
