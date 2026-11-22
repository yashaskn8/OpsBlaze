"""G2 adapter qualification proof: local bytes and signatures, no live effects."""

import base64
import hashlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

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
        identity_name = "status-outage" if name == "status-recovery" else name
        effect_id = str(uuid5(NAMESPACE_URL, "g2-disposable/" + identity_name))
        permit = hashlib.sha256(("permit/" + identity_name).encode()).hexdigest()

        def event(number, kind, observation):
            return {
                "schema": "opsblaze-g2-trace-v1",
                "scenario": name,
                "sequence": number,
                "effect_id": effect_id,
                "permit_sha256": permit,
                "event": kind,
                "observed": observation,
            }

        if state == "VERIFIED":
            status, oracle = "FOUND", "HEALTHY"
        elif state == "REJECTED":
            status, oracle = "MISSING", "UNHEALTHY"
        elif name == "status-outage":
            status, oracle = "UNAVAILABLE", "UNAVAILABLE"
        elif name == "retention-gap":
            status, oracle = "MISSING", "UNAVAILABLE"
        elif name == "oracle-outage":
            status, oracle = "FOUND", "UNAVAILABLE"
        else:
            status, oracle = "CONFLICT", "UNHEALTHY"

        observations = (
            [("DISPATCH", "SENT")] * attempt_min
            + [("APPLIED", "COMMITTED")] * applied
            + [("STATUS", status)]
            + [("ORACLE", oracle)]
        )
        raw = b"".join(
            (json.dumps(event(i, kind, obs), sort_keys=True) + "\n").encode()
            for i, (kind, obs) in enumerate(observations, 1)
        )
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


@pytest.mark.parametrize("change", [
    "forged_attempt_count", "false_verification", "mixed_effect_identity",
    "mixed_permit_identity", "nonsequential_events", "extra_sensitive_fields",
    "malformed_json", "duplicate_json_key", "forged_retention_observation",
    "false_oracle_success", "empty_event", "bad_schema", "boolean_sequence",
    "out_of_order", "recovery_uses_new_identity",
])
def test_redteam_even_rehashed_traces_must_support_signed_claims(tmp_path, change):
    """A matching trace hash cannot validate contradictory provider events."""
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    name = (
        "retention-gap" if change == "forged_retention_observation"
        else "status-recovery" if change == "recovery_uses_new_identity"
        else "first-apply"
    )
    path = traces / (name + ".jsonl")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if change == "forged_attempt_count":
        events = events[1:]
    elif change == "false_verification":
        events[-1]["observed"] = "UNAVAILABLE"
    elif change == "mixed_effect_identity":
        events[-1]["effect_id"] = str(uuid5(NAMESPACE_URL, "unrelated"))
    elif change == "mixed_permit_identity":
        events[1]["permit_sha256"] = "f" * 64
    elif change == "nonsequential_events":
        events[-1]["sequence"] = 1
    elif change == "extra_sensitive_fields":
        events[0]["provider_auth_token"] = "never-copy-secrets"
    elif change == "bad_schema":
        events[0]["schema"] = "unknown"
    elif change == "boolean_sequence":
        events[0]["sequence"] = True
    elif change == "forged_retention_observation":
        status = next(record for record in events if record["event"] == "STATUS")
        status["observed"] = "FOUND"
    elif change == "false_oracle_success":
        events[-1]["observed"] = "UNHEALTHY"
    elif change == "out_of_order":
        # Preserve the event set and numbering but reverse causal order.
        events[1], events[2] = events[2], events[1]
        for index, record in enumerate(events, 1):
            record["sequence"] = index
    elif change == "recovery_uses_new_identity":
        for record in events:
            record["effect_id"] = str(uuid5(NAMESPACE_URL, "different-effect"))
    raw = b"".join((json.dumps(record) + "\n").encode() for record in events)
    if change == "malformed_json":
        raw = b"{not-json}\n"
    elif change == "duplicate_json_key":
        raw = raw.replace(b'"schema":', b'"schema":"fake","schema":', 1)
    elif change == "empty_event":
        raw = b"\n" + raw
    path.write_bytes(raw)
    next(
        row for row in manifest["scenarios"] if row["scenario"] == name
    )["trace_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_trace_event_format_uses_no_raw_payloads(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    trace = (traces / "first-apply.jsonl").read_text()
    assert "secret" not in trace.lower()
    assert "Authorization" not in trace
    assert verify_qualification(
        manifest, traces, reviews, keys,
    )["status"] == "EVIDENCE_INTEGRITY_VALID_ONLY"


def test_redteam_symlink_race_bypass_cannot_redirect_evidence_read(
    tmp_path, monkeypatch,
):
    """Even a bypassed preliminary symlink check cannot follow a file."""
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    name = "first-apply.jsonl"
    original = traces / name
    external = tmp_path / "untrusted-but-byte-identical.jsonl"
    external.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(external)
    # A previous check could see a normal file, then a replacement
    # symlink. The descriptor-based O_NOFOLLOW check still rejects it.
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_redteam_hardlinked_trace_is_not_a_trusted_evidence_file(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    original = traces / "first-apply.jsonl"
    external = tmp_path / "duplicate-file-reference.jsonl"
    external.write_bytes(original.read_bytes())
    original.unlink()
    os.link(external, original)
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, traces, reviews, keys)


def test_redteam_symlinked_trace_directory_is_not_read(tmp_path):
    manifest, traces, reviews, keys = frozen_qualification(tmp_path)
    shortcut = tmp_path / "alias"
    shortcut.symlink_to(traces, target_is_directory=True)
    with pytest.raises(ValueError, match="qualification evidence invalid"):
        verify_qualification(manifest, shortcut, reviews, keys)
