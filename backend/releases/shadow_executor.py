"""Restricted declarative policy evaluator for isolated E6 shadow comparisons.

The evaluator accepts data-only rule documents. It has no imports of B2, P2, B3,
adapter gateways, network clients, or capability grants and cannot execute actions.
"""

from __future__ import annotations

from typing import Any

from backend.persistence.models import EvidenceLedger, PolicyBundle


def _lookup(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _matches(actual: Any, exists: bool, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return exists is (expected is True)
    if not exists:
        return False
    if operator == "equals":
        return actual == expected
    if operator == "contains":
        if not isinstance(actual, (str, list, dict)):
            return False
        return bool(expected in actual)
    if operator == "greater_than":
        return isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and actual > expected
    if operator == "less_than":
        return isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and actual < expected
    return False


def evaluate_bundle_shadow_policy(
    bundle: PolicyBundle,
    evidence: list[EvidenceLedger],
) -> dict[str, Any]:
    """Execute only the versioned JSON rule DSL stored in a sealed policy bundle."""
    diff = bundle.proposal_diff or {}
    policy = diff.get("shadow_policy") if isinstance(diff, dict) else None
    if not isinstance(policy, dict) or policy.get("version") != 1:
        return {
            "status": "NO_EXECUTABLE_POLICY",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": "Bundle has no supported shadow_policy version 1 rule document.",
            "matched_rule_id": None,
            "evidence_ids": [],
        }

    rules = policy.get("rules")
    if not isinstance(rules, list) or len(rules) > 100:
        return {
            "status": "INVALID_POLICY",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": "shadow_policy.rules must be a list containing at most 100 rules.",
            "matched_rule_id": None,
            "evidence_ids": [],
        }

    ordered_evidence = sorted(evidence, key=lambda item: (item.observed_at, str(item.id)))
    for rule_index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id", f"rule-{rule_index + 1}")
        path = rule.get("path")
        operator = rule.get("operator")
        expected = rule.get("value")
        decision = rule.get("decision", "HYPOTHESIS")
        hypothesis = rule.get("hypothesis")
        rationale = rule.get("rationale", "")
        confidence = rule.get("confidence", 0.0)
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 256
            or operator not in {"exists", "equals", "contains", "greater_than", "less_than"}
            or decision not in {"HYPOTHESIS", "ABSTAIN"}
            or not isinstance(hypothesis, str) and decision == "HYPOTHESIS"
            or not isinstance(rationale, str)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            continue
        for record in ordered_evidence:
            payload = record.raw_payload_redacted
            exists, actual = _lookup(payload if isinstance(payload, dict) else {}, path)
            if _matches(actual, exists, str(operator), expected):
                return {
                    "status": "EVALUATED",
                    "decision": decision,
                    "hypothesis": hypothesis[:256] if isinstance(hypothesis, str) else None,
                    "confidence": float(confidence),
                    "rationale": rationale[:1024],
                    "matched_rule_id": str(rule_id)[:128],
                    "evidence_ids": [str(record.id)],
                }

    default = policy.get("default")
    if isinstance(default, dict) and default.get("decision") == "ABSTAIN":
        return {
            "status": "EVALUATED",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": str(default.get("rationale", "No rule matched."))[:1024],
            "matched_rule_id": "default",
            "evidence_ids": [],
        }
    return {
        "status": "NO_RULE_MATCH",
        "decision": "ABSTAIN",
        "hypothesis": None,
        "confidence": 0.0,
        "rationale": "No candidate rule matched the available valid evidence.",
        "matched_rule_id": None,
        "evidence_ids": [],
    }
