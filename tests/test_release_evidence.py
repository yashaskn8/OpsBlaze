"""G5 independent-key review evidence without any promotion authority."""

import base64
import hashlib
import json
import sys
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.evaluation.heldout import canonical_digest, evaluate_heldout
from backend.evaluation.preregistration import (
    SCHEMA, PRIMARY_METRIC, STOPPING_RULE, registration_bytes,
    verify_preregistration,
)
from backend.evaluation.recovery_cost import measured_recovery_cost
from backend.policy.release_evidence import (
    ACTION, ROLES, main, review_bytes, recompute_signed_e5,
    verify_release_evidence,
)


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
            "witness_sha256": (
                hashlib.sha256(f"independent/{arm}/{i}".encode()).hexdigest()
                if result == "VERIFIED" else None
            ),
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


def review_fixture():
    bundle = {
        "policy_content": {
            "candidate_id": "synthetic-candidate",
            "tool_catalog": "simulator-readonly-v1",
            "model_id": "none",
        },
        "source_commit": "1" * 40,
        "tool_manifest_digest": "2" * 64,
    }
    bundle["policy_digest"] = canonical_digest(bundle["policy_content"])
    manifest, oracle, trials, measurements = recovery_fixture()
    prereg_signer = Ed25519PrivateKey.generate()
    prereg_raw = prereg_signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    prereg_public = prereg_signer.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    registration = {
        "schema": SCHEMA,
        "manifest_sha256": canonical_digest(manifest),
        "signer_public_key_sha256": hashlib.sha256(prereg_raw).hexdigest(),
        "signed_at_utc": "2026-11-22T08:00:00Z",
        "expected_pairs": 64,
        "primary_metric": PRIMARY_METRIC,
        "stopping_rule": STOPPING_RULE,
        "arm_versions": {
            "candidate": "simulator-shadow-v1",
            "incumbent": "simulator-runbook-v1",
        },
    }
    registration["signature"] = base64.urlsafe_b64encode(
        prereg_signer.sign(registration_bytes(registration)),
    ).decode().rstrip("=")
    frozen_inputs = {
        "manifest": manifest, "oracle": oracle, "trials": trials,
        "preregistration": registration, "measurements": measurements,
    }
    evaluation = recompute_signed_e5(frozen_inputs, prereg_public)
    keys, public = {}, {}
    for role in ROLES:
        key = Ed25519PrivateKey.generate()
        keys[role] = key
        public[role] = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    reviews = []
    for role in ROLES:
        raw = keys[role].public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        record = {
            "version": 1,
            "role": role,
            "policy_digest": bundle["policy_digest"],
            "evaluation_digest": canonical_digest(evaluation),
            "source_commit": bundle["source_commit"],
            "tool_manifest_digest": bundle["tool_manifest_digest"],
            "signer_key_sha256": hashlib.sha256(raw).hexdigest(),
            "review_action": ACTION,
            "signed_at_utc": "2026-11-22T08:00:00Z",
        }
        record["signature"] = base64.urlsafe_b64encode(
            keys[role].sign(review_bytes(record))
        ).decode().rstrip("=")
        reviews.append(record)
    return bundle, evaluation, reviews, public, frozen_inputs, prereg_public


def test_distinct_pinned_review_keys_bind_one_frozen_evidence_set():
    bundle, evaluation, reviews, keys, inputs, prereg_key = review_fixture()
    report = verify_release_evidence(
        bundle, evaluation, reviews, keys,
        frozen_inputs=inputs, trusted_preregistration_key=prereg_key,
    )
    assert report["state"] == "REVIEW_SIGNATURES_VALID_ONLY"
    assert report["offline_e5_report_recomputed"] is True
    assert len(set(report["reviewer_key_fingerprints"].values())) == 3
    assert report["evaluation_digest"] == canonical_digest(evaluation)
    assert report["production_gate_passed"] is False
    assert report["external_role_custody_certified"] is False
    assert report["trusted_timestamp_certified"] is False
    assert report["deployment_authorized"] is False
    assert report["promotion_eligible"] is False


@pytest.mark.parametrize("change", [
    "bundle_content", "bundle_digest", "report", "wrong_key", "duplicate_role",
    "missing_role", "same_actor", "changed_action", "changed_digest",
    "changed_commit", "changed_timestamp", "extra_field", "bad_signature",
    "claimed_promotion", "missing_preregistration", "no_recovery_measurements",
])
def test_tampered_or_untrusted_release_evidence_is_denied(change):
    bundle, evaluation, reviews, trusted, inputs, prereg_key = review_fixture()
    if change == "bundle_content":
        bundle["policy_content"]["tool_catalog"] = "unreviewed-external-adapter"
    elif change == "bundle_digest":
        bundle["policy_digest"] = "f" * 64
    elif change == "report":
        evaluation["recovery_cost"]["pairs"] = 12
    elif change == "wrong_key":
        trusted["release_owner"] = trusted["evaluator"]
    elif change == "duplicate_role":
        reviews[-1] = deepcopy(reviews[0])
    elif change == "missing_role":
        reviews.pop()
    elif change == "same_actor":
        trusted["security_reviewer"] = trusted["evaluator"]
    elif change == "changed_action":
        reviews[0]["review_action"] = "APPROVE_DEPLOYMENT"
    elif change == "changed_digest":
        reviews[1]["evaluation_digest"] = "f" * 64
    elif change == "changed_commit":
        reviews[2]["source_commit"] = "f" * 40
    elif change == "changed_timestamp":
        reviews[0]["signed_at_utc"] = "2026-11-23T08:00:00Z"
    elif change == "extra_field":
        reviews[1]["override_gate"] = True
    elif change == "bad_signature":
        reviews[0]["signature"] = "!" * 86
    elif change == "claimed_promotion":
        evaluation["promotion_eligible"] = True
    elif change == "missing_preregistration":
        evaluation["preregistration"]["status"] = "UNSIGNED_LAB_ONLY"
    else:
        evaluation.pop("recovery_cost")
    with pytest.raises(ValueError, match="invalid"):
        verify_release_evidence(
            bundle, evaluation, reviews, trusted,
            frozen_inputs=inputs, trusted_preregistration_key=prereg_key,
        )


def test_cli_produces_only_non_authorizing_review_receipt(tmp_path, monkeypatch, capsys):
    bundle, evaluation, reviews, trusted, inputs, prereg_key = review_fixture()
    for name, data in [
        ("bundle", bundle), ("evaluation", evaluation),
        ("reviews", {"reviews": reviews}),
    ]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    for role in ROLES:
        (tmp_path / f"{role}.pem").write_bytes(trusted[role])
    for name, data in inputs.items():
        (tmp_path / f"{name}.json").write_text(
            json.dumps(data), encoding="utf-8",
        )
    (tmp_path / "trusted-preregistration.pem").write_bytes(prereg_key)
    args = [
        "release-evidence",
        "--bundle", str(tmp_path / "bundle.json"),
        "--evaluation", str(tmp_path / "evaluation.json"),
        "--reviews", str(tmp_path / "reviews.json"),
        "--evaluator-key", str(tmp_path / "evaluator.pem"),
        "--security-key", str(tmp_path / "security_reviewer.pem"),
        "--release-owner-key", str(tmp_path / "release_owner.pem"),
        "--manifest", str(tmp_path / "manifest.json"),
        "--oracle", str(tmp_path / "oracle.json"),
        "--trials", str(tmp_path / "trials.json"),
        "--preregistration", str(tmp_path / "preregistration.json"),
        "--recovery-measurements", str(tmp_path / "measurements.json"),
        "--trusted-preregistration-key", str(
            tmp_path / "trusted-preregistration.pem"
        ),
    ]
    monkeypatch.setattr(sys, "argv", args)
    main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "REVIEW_SIGNATURES_VALID_ONLY"
    assert receipt["offline_e5_report_recomputed"] is True
    assert not receipt["deployment_authorized"]

    reviews[0]["evaluation_digest"] = "f" * 64
    (tmp_path / "reviews.json").write_text(
        json.dumps({"reviews": reviews}), encoding="utf-8",
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    denied = json.loads(capsys.readouterr().out)
    assert denied == {
        "state": "INVALID",
        "deployment_authorized": False,
        "promotion_eligible": False,
    }


@pytest.mark.parametrize("change", [
    "missing_oracle", "forged_preregistration_key", "altered_manifest",
    "altered_trial", "tampered_preregistration", "repeated_witness",
    "cost_regression", "inflated_observation_count",
])
def test_redteam_recomputed_e5_rejects_false_or_missing_source_evidence(change):
    _bundle, _evaluation, _reviews, _trusted, original, prereg_key = review_fixture()
    inputs = deepcopy(original)
    if change == "missing_oracle":
        inputs.pop("oracle")
    elif change == "forged_preregistration_key":
        prereg_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    elif change == "altered_manifest":
        inputs["manifest"]["min_pairs"] -= 1
    elif change == "altered_trial":
        inputs["trials"]["candidate"][0]["decision"] = "ABSTAIN"
    elif change == "tampered_preregistration":
        inputs["preregistration"]["expected_pairs"] = 65
    elif change == "repeated_witness":
        observations = inputs["measurements"]["arms"]["candidate"]
        observations[1]["witness_sha256"] = observations[0]["witness_sha256"]
    elif change == "cost_regression":
        inputs["measurements"]["arms"]["candidate"][0][
            "provider_microcredits"
        ] += 10000
    else:
        inputs["measurements"]["arms"]["candidate"].pop()
    with pytest.raises(ValueError):
        recompute_signed_e5(inputs, prereg_key)


def test_redteam_genuine_signatures_cannot_replace_underlying_e5_proof():
    bundle, evaluation, reviews, trusted, inputs, prereg_key = review_fixture()
    fake = deepcopy(evaluation)
    fake["recovery_cost"]["arms"]["candidate"]["verified"] = 5000
    fake["verified_recovery_per_cost"]["candidate"] = 9999999.0

    # Even if the signer keys were valid and the reviewers knowingly
    # signed this forged report, source recomputation must refuse it.
    for role, record in zip(ROLES, reviews):
        key = Ed25519PrivateKey.generate()
        public = key.public_key()
        trusted[role] = public.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        record["evaluation_digest"] = canonical_digest(fake)
        record["signer_key_sha256"] = hashlib.sha256(
            public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        ).hexdigest()
        record["signature"] = base64.urlsafe_b64encode(
            key.sign(review_bytes(record)),
        ).decode().rstrip("=")

    with pytest.raises(ValueError, match="invalid"):
        verify_release_evidence(
            bundle, fake, reviews, trusted,
            frozen_inputs=inputs, trusted_preregistration_key=prereg_key,
        )


def test_recomputed_e5_preserves_non_promotable_result_and_oracle_digest():
    _bundle, original, _reviews, _trusted, inputs, prereg_key = review_fixture()
    assert evaluate_heldout(
        inputs["manifest"], inputs["oracle"], inputs["trials"],
    )["state"] == "OFFLINE_PASS"
    assert verify_preregistration(
        inputs["manifest"], inputs["preregistration"], prereg_key,
    )["status"] == "VALID_SIGNATURE"
    assert measured_recovery_cost(
        inputs["oracle"], inputs["trials"], inputs["measurements"],
    )["promotion_eligible"] is False
    recomputed = recompute_signed_e5(inputs, prereg_key)
    assert recomputed == original
    assert recomputed["promotion_eligible"] is False
    assert "cases" not in recomputed


def test_redteam_json_number_type_masquerade_is_not_exact_report_match():
    bundle, evaluation, reviews, trusted, inputs, prereg_key = review_fixture()
    forged = deepcopy(evaluation)
    verified_count = forged["recovery_cost"]["arms"]["candidate"]["verified"]
    assert type(verified_count) is int
    forged["recovery_cost"]["arms"]["candidate"]["verified"] = float(
        verified_count
    )
    assert forged == evaluation  # Python considers 30 and 30.0 equal.
    assert canonical_digest(forged) != canonical_digest(evaluation)
    with pytest.raises(ValueError, match="invalid"):
        verify_release_evidence(
            bundle, forged, reviews, trusted,
            frozen_inputs=inputs, trusted_preregistration_key=prereg_key,
        )
