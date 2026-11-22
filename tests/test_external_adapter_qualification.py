"""G2 adapter qualification proof: local bytes and signatures, no live effects."""

import base64
import hashlib
import json
import sys
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.adapters.qualification import (
    ACTION, ROLES, SCENARIOS, SCHEMA, main, review_bytes,
    verify_qualification,
)
from backend.evaluation.heldout import canonical_digest


def frozen_qualification(tmp_path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    rows = []
    for name, (state, applied, attempt_min) in SCENARIOS.items():
        raw = f"synthetic-redacted-event={name}\n".encode()
        (trace_dir / (name + ".jsonl")).write_bytes(raw)
        rows.append({
            "scenario": name,
            "result": state,
            "dispatch_attempts": attempt_min,
            "applied_effect_count": applied,
            "status_queries": 1,
            "oracle_queries": 1,
            "trace_sha256": hashlib.sha256(raw).hexdigest(),
        })
    manifest = {
        "schema": SCHEMA,
        "adapter_id": "example-adapter",
        "provider_id": "example-provider",
        "environment_id": "authorized-disposable-lab",
        "target_family": "disposable-target",
        "effect_action": "approved-repair",
        "source_commit": "a" * 40,
        "adapter_code_sha256": "b" * 64,
        "adapter_config_sha256": "c" * 64,
        "provider_contract_sha256": "d" * 64,
        "status_reader_sha256": "e" * 64,
        "postcondition_reader_sha256": "f" * 64,
        "fencing_contract": "ATOMIC_FENCE_AND_CAS",
        "idempotency_retention_seconds": 86400,
        "max_retry_seconds": 3600,
        "scenarios": rows,
    }
    owners = {role: Ed25519PrivateKey.generate() for role in ROLES}
    trusted = {
        role: owner.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        for role, owner in owners.items()
    }
    reviews = []
    for role, owner in owners.items():
        raw_key = owner.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        record = {
            "role": role,
            "manifest_sha256": canonical_digest(manifest),
            "signer_key_sha256": hashlib.sha256(raw_key).hexdigest(),
            "review_action": ACTION,
            "signed_at_utc": "2026-11-22T12:00:00Z",
        }
        record["signature"] = base64.urlsafe_b64encode(
            owner.sign(review_bytes(record)),
        ).decode().rstrip("=")
        reviews.append(record)
    return manifest, trace_dir, reviews, trusted


def test_sealed_provider_evidence_has_three_distinct_review_signatures(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    result = verify_qualification(manifest, traces, reviews, keys)
    assert result["status"] == "EVIDENCE_INTEGRITY_VALID_ONLY"
    assert result["scenario_count"] == len(SCENARIOS)
    assert result["manifest_sha256"] == canonical_digest(manifest)
    assert len(set(result["reviewer_key_fingerprints"].values())) == 3
    assert result["provider_execution_certified"] is False
    assert result["independent_review_certified"] is False
    assert result["external_effects_enabled"] is False
    assert result["production_gate_passed"] is False
    assert "traces" not in result and "effect_action" not in result


@pytest.mark.parametrize("change", [
    "different_effect_action", "different_source_commit",
    "missing_scenario", "duplicate_scenario", "unknown_scenario",
    "pretend_success_on_stale_fence", "duplicate_effect",
    "blind_retry_on_lost_ack", "no_status_oracle", "no_independent_oracle",
    "shared_observer", "idempotency_retention_too_short",
    "retry_exceeds_retention", "boolean_attempt", "invalid_hash",
    "production_environment", "simulator_adapter", "extra_field",
])
def test_unsafe_or_tampered_provider_contract_is_rejected(tmp_path, change):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    manifest = deepcopy(manifest)
    rows = {row["scenario"]: row for row in manifest["scenarios"]}
    if change == "different_effect_action":
        manifest["effect_action"] = "other-repair"
    elif change == "different_source_commit":
        manifest["source_commit"] = "0" * 40
    elif change == "missing_scenario":
        manifest["scenarios"].pop()
    elif change == "duplicate_scenario":
        manifest["scenarios"][-1] = deepcopy(manifest["scenarios"][0])
    elif change == "unknown_scenario":
        rows["first-apply"]["scenario"] = "../../secrets"
    elif change == "pretend_success_on_stale_fence":
        rows["stale-fence"]["result"] = "VERIFIED"
    elif change == "duplicate_effect":
        rows["duplicate-call"]["applied_effect_count"] = 2
    elif change == "blind_retry_on_lost_ack":
        rows["lost-reply-after-commit"]["dispatch_attempts"] = 2
    elif change == "no_status_oracle":
        rows["status-outage"]["status_queries"] = 0
    elif change == "no_independent_oracle":
        rows["first-apply"]["oracle_queries"] = 0
    elif change == "shared_observer":
        manifest["postcondition_reader_sha256"] = manifest["status_reader_sha256"]
    elif change == "idempotency_retention_too_short":
        manifest["idempotency_retention_seconds"] = 42
    elif change == "retry_exceeds_retention":
        manifest["max_retry_seconds"] = 86400
    elif change == "boolean_attempt":
        rows["first-apply"]["dispatch_attempts"] = True
    elif change == "invalid_hash":
        rows["first-apply"]["trace_sha256"] = "?" * 64
    elif change == "production_environment":
        manifest["environment_id"] = "production-live"
    elif change == "simulator_adapter":
        manifest["adapter_id"] = "simulator-example"
    else:
        manifest["emergency_bypass"] = True
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


@pytest.mark.parametrize("change", [
    "missing_witness", "duplicate_role", "wrong_owner_key",
    "shared_key", "tampered_signature", "self_authorizing_action",
    "modified_timestamp", "extraneous_claim", "invalid_base64",
])
def test_untrusted_evidence_review_is_rejected(tmp_path, change):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    reviews = deepcopy(reviews)
    keys = dict(keys)
    if change == "missing_witness":
        reviews.pop(0)
    elif change == "duplicate_role":
        reviews[-1] = deepcopy(reviews[0])
    elif change == "wrong_owner_key":
        keys["release_owner"] = Ed25519PrivateKey.generate().public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    elif change == "shared_key":
        keys["release_owner"] = keys["security_reviewer"]
    elif change == "tampered_signature":
        reviews[0]["manifest_sha256"] = "0" * 64
    elif change == "self_authorizing_action":
        reviews[1]["review_action"] = "ACTIVATE_REAL_EFFECTS"
    elif change == "modified_timestamp":
        reviews[2]["signed_at_utc"] = "2026-11-22T13:00:00Z"
    elif change == "extraneous_claim":
        reviews[0]["production_gate_passed"] = True
    else:
        reviews[0]["signature"] = "!" * 86
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_modified_or_missing_provider_traces_fail_closed(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    path = traces / "first-apply.jsonl"
    path.write_text("altered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)
    path.unlink()
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)
    path.symlink_to(traces / "duplicate-call.jsonl")
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_oversized_trace_is_not_ingested(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    (traces / "first-apply.jsonl").write_bytes(b"x" * (2_000_000 + 1))
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_cli_reports_only_non_authorizing_evidence_and_denies_tampering(
    tmp_path, monkeypatch, capsys,
):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "reviews.json").write_text(
        json.dumps({"reviews": reviews}), encoding="utf-8",
    )
    for role, pem in keys.items():
        (tmp_path / f"{role}.pem").write_bytes(pem)
    argv = [
        "qualification", "--manifest", str(tmp_path / "manifest.json"),
        "--traces-dir", str(traces),
        "--reviews", str(tmp_path / "reviews.json"),
        "--provider-key", str(tmp_path / "provider_witness.pem"),
        "--security-key", str(tmp_path / "security_reviewer.pem"),
        "--release-owner-key", str(tmp_path / "release_owner.pem"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    main()
    valid = json.loads(capsys.readouterr().out)
    assert valid["status"] == "EVIDENCE_INTEGRITY_VALID_ONLY"
    assert not valid["external_effects_enabled"]
    manifest["adapter_code_sha256"] = "1" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid == {
        "status": "INVALID", "production_gate_passed": False,
        "external_effects_enabled": False,
    }


def test_redteam_adapter_evidence_cannot_grant_external_capabilities(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    for row in manifest["scenarios"]:
        row["trace_sha256"] = hashlib.sha256(
            (traces / (row["scenario"] + ".jsonl")).read_bytes(),
        ).hexdigest()
    result = verify_qualification(manifest, traces, reviews, keys)
    assert result["external_effects_enabled"] is False
    assert result["production_gate_passed"] is False
    assert "deployment_authorized" not in result
