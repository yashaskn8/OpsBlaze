"""Tests for the restricted declarative E6 shadow-policy evaluator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from backend.persistence.models import EvidenceLedger, PolicyBundle
from backend.releases.shadow_executor import evaluate_bundle_shadow_policy


def _bundle(proposal_diff: dict) -> PolicyBundle:
    return PolicyBundle(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        version=1,
        bundle_name="shadow-candidate",
        bundle_sha256="a" * 64,
        signature="b" * 128,
        signature_key_id="test-key",
        base_policy_version="v1",
        target_component="investigator_planner",
        proposal_diff=proposal_diff,
        prompt_templates={},
        active_lesson_ids=[],
        model_id="deterministic-shadow-dsl",
        model_version="1",
        tool_catalog_version="1",
        a2_grant_version="1",
        source_provenance_digest="c" * 64,
        status="SEALED",
        created_by="test",
    )


def _evidence(payload: dict) -> EvidenceLedger:
    return EvidenceLedger(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        source="test",
        source_version="1",
        target_id="target-1",
        target_version="v1",
        observed_at=datetime.now(UTC),
        sha256_digest="d" * 64,
        raw_payload_redacted=payload,
        classification="TELEMETRY",
        validity_status="VALID",
    )


def test_candidate_shadow_rule_executes_against_verified_evidence() -> None:
    bundle = _bundle(
        {
            "shadow_policy": {
                "version": 1,
                "rules": [
                    {
                        "id": "high-cpu",
                        "path": "metrics.cpu_percent",
                        "operator": "greater_than",
                        "value": 90,
                        "decision": "HYPOTHESIS",
                        "hypothesis": "CPU saturation",
                        "confidence": 0.91,
                        "rationale": "CPU telemetry crossed the candidate threshold.",
                    }
                ],
            }
        }
    )
    evidence = [_evidence({"metrics": {"cpu_percent": 96}})]

    result = evaluate_bundle_shadow_policy(bundle, evidence)

    assert result["status"] == "EVALUATED"
    assert result["decision"] == "HYPOTHESIS"
    assert result["hypothesis"] == "CPU saturation"
    assert result["matched_rule_id"] == "high-cpu"
    assert len(result["evidence_ids"]) == 1


def test_shadow_policy_without_executable_rules_abstains() -> None:
    result = evaluate_bundle_shadow_policy(_bundle({}), [_evidence({"cpu": 99})])

    assert result["status"] == "NO_EXECUTABLE_POLICY"
    assert result["decision"] == "ABSTAIN"
    assert result["confidence"] == 0.0


def test_shadow_policy_rejects_unsupported_operators_without_execution() -> None:
    bundle = _bundle(
        {
            "shadow_policy": {
                "version": 1,
                "rules": [
                    {
                        "id": "unsafe",
                        "path": "cpu",
                        "operator": "python_eval",
                        "value": "True",
                        "decision": "HYPOTHESIS",
                        "hypothesis": "unsafe",
                        "confidence": 1.0,
                        "rationale": "must never execute",
                    }
                ],
            }
        }
    )

    result = evaluate_bundle_shadow_policy(bundle, [_evidence({"cpu": 99})])

    assert result["status"] == "NO_RULE_MATCH"
    assert result["decision"] == "ABSTAIN"
