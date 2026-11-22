"""G5 offline three-authority release-evidence signature review.

This module ONLY validates that three independently pinned keys signed the
same frozen artifact digests. It has no endpoint, database write, or way
to enable policy assignment, a production target, or an effect.
"""

import argparse
import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.evaluation.heldout import (
    canonical_digest, evaluate_heldout, read_json,
)
from backend.evaluation.preregistration import (
    load_trusted_public_key, verify_preregistration,
)
from backend.evaluation.recovery_cost import measured_recovery_cost

ROLES = ("evaluator", "security_reviewer", "release_owner")
ACTION = "ACKNOWLEDGE_EVIDENCE_ONLY"
VERSION = 1
REVIEW_FIELDS = frozenset({
    "version", "role", "policy_digest", "evaluation_digest",
    "source_commit", "tool_manifest_digest", "signer_key_sha256",
    "review_action", "signed_at_utc", "signature",
})
BUNDLE_FIELDS = frozenset({
    "policy_content", "policy_digest", "source_commit",
    "tool_manifest_digest",
})


def _hex(value: object, n: int) -> bool:
    return (
        isinstance(value, str) and len(value) == n
        and all(c in "0123456789abcdef" for c in value)
    )


def review_bytes(record: dict) -> bytes:
    """Explicitly domain-separate a non-authorizing G5 evidence review."""
    return (
        b"OpsBlaze G5 evidence review only v1\n"
        + json.dumps(
            {k: v for k, v in record.items() if k != "signature"},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    )


def _trusted_key(pem: bytes) -> tuple[Ed25519PublicKey, str]:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("reviewer public key must be Ed25519")
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, hashlib.sha256(raw).hexdigest()


def recompute_signed_e5(
    frozen_inputs: dict, trusted_preregistration_key: bytes,
) -> dict:
    """Recalculate the *entire* offline E5 result from separately held files.

    A signed or self-asserted OFFLINE_PASS is insufficient. Trusted key
    custody and actual trial independence still require external review.
    """
    required = {
        "manifest", "oracle", "trials", "preregistration", "measurements",
    }
    if not isinstance(frozen_inputs, dict) or set(frozen_inputs) != required:
        raise ValueError("all five sealed E5 inputs are required")
    manifest = frozen_inputs["manifest"]
    oracle = frozen_inputs["oracle"]
    trials = frozen_inputs["trials"]
    registration = frozen_inputs["preregistration"]
    measurements = frozen_inputs["measurements"]

    proof = verify_preregistration(
        manifest, registration, trusted_preregistration_key,
    )
    if (
        not isinstance(oracle, dict)
        or not isinstance(oracle.get("cases"), list)
        or len(oracle["cases"]) != proof["expected_pairs"]
    ):
        raise ValueError("signed sample count does not match E5 oracle")
    report = evaluate_heldout(manifest, oracle, trials)
    if report["state"] != "OFFLINE_PASS":
        raise ValueError("underlying blinded trial is not OFFLINE_PASS")
    measured = measured_recovery_cost(oracle, trials, measurements)
    if (
        measured["pairs"] != proof["expected_pairs"]
        or measured["candidate_ratio_higher"] is not True
        or measured["regressed_families"]
        or measured["cost_regressed_families"]
    ):
        raise ValueError("real recovery/cost measurements regress or do not improve")
    report["preregistration"] = proof
    report["recovery_cost"] = measured
    report["verified_recovery_per_cost"] = {
        arm: measured["arms"][arm]["verified_per_million_microcredits"]
        for arm in ("candidate", "incumbent")
    }
    return report


def verify_release_evidence(
    bundle: dict, evaluation: dict, reviews: list[dict],
    trusted_keys: dict[str, bytes],
    *,
    frozen_inputs: dict,
    trusted_preregistration_key: bytes,
) -> dict:
    """Verify frozen content and three *distinct* externally trusted keys.

    Signature and document integrity do not prove human roles, independent
    custody, true signing time or passed production release gates.
    """
    try:
        recalculated = recompute_signed_e5(
            frozen_inputs, trusted_preregistration_key,
        )
        if (
            not isinstance(evaluation, dict)
            or canonical_digest(evaluation) != canonical_digest(recalculated)
        ):
            # JSON bool/int and int/float can compare equal in Python.
            # Compare typed canonical bytes, not Python dictionary equality.
            raise ValueError("reviewed E5 result is not reproducible")
        if (
            not isinstance(bundle, dict) or set(bundle) != BUNDLE_FIELDS
            or not isinstance(bundle["policy_content"], dict)
            or not _hex(bundle["source_commit"], 40)
            or not _hex(bundle["policy_digest"], 64)
            or not _hex(bundle["tool_manifest_digest"], 64)
            or bundle["policy_digest"] != canonical_digest(bundle["policy_content"])
            or not isinstance(evaluation, dict)
            or evaluation.get("state") != "OFFLINE_PASS"
            or evaluation.get("promotion_eligible") is not False
            or not isinstance(evaluation.get("recovery_cost"), dict)
            or evaluation["recovery_cost"].get("promotion_eligible") is not False
            or not isinstance(evaluation.get("preregistration"), dict)
            or evaluation["preregistration"].get("status") != "VALID_SIGNATURE"
            or not isinstance(reviews, list) or len(reviews) != len(ROLES)
            or not isinstance(trusted_keys, dict)
            or set(trusted_keys) != set(ROLES)
        ):
            raise ValueError("invalid frozen release evidence")
        evaluation_digest = canonical_digest(evaluation)
        by_role = {}
        for review in reviews:
            if (
                not isinstance(review, dict) or set(review) != REVIEW_FIELDS
                or review.get("role") not in ROLES
                or review["role"] in by_role
            ):
                raise ValueError("missing, extra or duplicate review authority")
            by_role[review["role"]] = review
        if set(by_role) != set(ROLES):
            raise ValueError("missing independent authority")

        fingerprints = {}
        for role in ROLES:
            record = by_role[role]
            key, fingerprint = _trusted_key(trusted_keys[role])
            if (
                type(record["version"]) is not int or record["version"] != VERSION
                or record["review_action"] != ACTION
                or record["policy_digest"] != bundle["policy_digest"]
                or record["evaluation_digest"] != evaluation_digest
                or record["source_commit"] != bundle["source_commit"]
                or record["tool_manifest_digest"] != bundle["tool_manifest_digest"]
                or record["signer_key_sha256"] != fingerprint
            ):
                raise ValueError("review does not match frozen evidence")
            signed = record["signed_at_utc"]
            if (
                not isinstance(signed, str)
                or not 20 <= len(signed) <= 35
                or not signed.endswith("Z")
                or datetime.fromisoformat(
                    signed.replace("Z", "+00:00"),
                ).utcoffset() != timedelta(0)
            ):
                raise ValueError("invalid UTC signing claim")
            sig = record["signature"]
            if not isinstance(sig, str) or len(sig) != 86:
                raise ValueError("invalid detached review signature")
            decoded = base64.b64decode(sig + "==", altchars=b"-_", validate=True)
            if (
                len(decoded) != 64
                or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != sig
            ):
                raise ValueError("noncanonical detached review signature")
            key.verify(decoded, review_bytes(record))
            fingerprints[role] = fingerprint
        if len(set(fingerprints.values())) != len(ROLES):
            raise ValueError("different roles reused one signing key")
    except (
        ValueError, TypeError, KeyError, AttributeError, InvalidSignature,
        binascii.Error, UnicodeError,
    ) as exc:
        raise ValueError("offline release review evidence invalid") from exc

    return {
        "state": "REVIEW_SIGNATURES_VALID_ONLY",
        "policy_digest": bundle["policy_digest"],
        "evaluation_digest": evaluation_digest,
        "source_commit": bundle["source_commit"],
        "tool_manifest_digest": bundle["tool_manifest_digest"],
        "reviewer_key_fingerprints": fingerprints,
        "offline_e5_report_recomputed": True,
        "external_role_custody_certified": False,
        "trusted_timestamp_certified": False,
        "production_gate_passed": False,
        "promotion_eligible": False,
        "deployment_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline G5 evidence signatures (never enables deployment)"
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--reviews", required=True)
    parser.add_argument("--evaluator-key", required=True)
    parser.add_argument("--security-key", required=True)
    parser.add_argument("--release-owner-key", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--oracle", required=True)
    parser.add_argument("--trials", required=True)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--recovery-measurements", required=True)
    parser.add_argument("--trusted-preregistration-key", required=True)
    args = parser.parse_args()
    try:
        records = read_json(args.reviews)
        if set(records) != {"reviews"}:
            raise ValueError("invalid review container")
        proof = verify_release_evidence(
            read_json(args.bundle), read_json(args.evaluation),
            records["reviews"],
            {
                "evaluator": load_trusted_public_key(args.evaluator_key),
                "security_reviewer": load_trusted_public_key(args.security_key),
                "release_owner": load_trusted_public_key(args.release_owner_key),
            },
            frozen_inputs={
                "manifest": read_json(args.manifest),
                "oracle": read_json(args.oracle),
                "trials": read_json(args.trials),
                "preregistration": read_json(args.preregistration),
                "measurements": read_json(args.recovery_measurements),
            },
            trusted_preregistration_key=load_trusted_public_key(
                args.trusted_preregistration_key,
            ),
        )
    except (OSError, ValueError, TypeError):
        proof = {
            "state": "INVALID",
            "promotion_eligible": False,
            "deployment_authorized": False,
        }
    print(json.dumps(proof, indent=2, sort_keys=True))
    if proof["state"] == "INVALID":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
