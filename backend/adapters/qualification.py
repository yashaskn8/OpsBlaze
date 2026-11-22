"""G2 offline external-adapter qualification evidence verifier.

Accepts local trace files and externally pinned reviewer keys. This does not
call a provider, trust witness independence, or grant any effect capability.
"""

import argparse
import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.evaluation.heldout import canonical_digest, read_json
from backend.evaluation.preregistration import load_trusted_public_key

SCHEMA = "opsblaze-g2-qualification-v1"
ROLES = ("provider_witness", "security_reviewer", "release_owner")
ACTION = "REVIEW_EVIDENCE_ONLY"
MAX_TRACE_BYTES = 2_000_000
SCENARIOS = {
    "first-apply": ("VERIFIED", 1, 1),
    "duplicate-call": ("VERIFIED", 1, 2),
    "competing-workers": ("VERIFIED", 1, 2),
    "stale-fence": ("REJECTED", 0, 1),
    "target-version-cas": ("REJECTED", 0, 1),
    "permit-expiry": ("REJECTED", 0, 1),
    "external-edit": ("REJECTED", 0, 1),
    "effect-identity-mismatch": ("REJECTED", 0, 1),
    "lost-reply-after-commit": ("VERIFIED", 1, 1),
    "status-outage": ("UNKNOWN", 1, 1),
    "oracle-outage": ("UNKNOWN", 1, 1),
    "partial-apply": ("NEEDS_REVIEW", 1, 1),
    "retention-gap": ("UNKNOWN", 1, 1),
    "status-recovery": ("VERIFIED", 1, 1),
}
MANIFEST_FIELDS = frozenset({
    "schema", "adapter_id", "provider_id", "environment_id", "target_family",
    "effect_action", "source_commit", "adapter_code_sha256",
    "adapter_config_sha256", "provider_contract_sha256",
    "status_reader_sha256", "postcondition_reader_sha256",
    "fencing_contract", "idempotency_retention_seconds",
    "max_retry_seconds", "scenarios",
})
SCENARIO_FIELDS = frozenset({
    "scenario", "result", "dispatch_attempts", "applied_effect_count",
    "status_queries", "oracle_queries", "trace_sha256",
})
REVIEW_FIELDS = frozenset({
    "role", "manifest_sha256", "signer_key_sha256",
    "review_action", "signed_at_utc", "signature",
})
SLUG = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")


def _hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length
        and all(c in "0123456789abcdef" for c in value)
    )


def review_bytes(review: dict) -> bytes:
    """Never confuse a G2 review with a G5 production release signature."""
    return (
        b"OpsBlaze G2 external adapter evidence review only v1\n"
        + json.dumps(
            {key: value for key, value in review.items() if key != "signature"},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    )


def _check_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("invalid qualification manifest schema")
    if manifest["schema"] != SCHEMA:
        raise ValueError("unsupported qualification schema")
    for field in ("adapter_id", "provider_id", "environment_id",
                  "target_family", "effect_action"):
        if not isinstance(manifest[field], str) or not SLUG.fullmatch(manifest[field]):
            raise ValueError("invalid provider/target identifiers")
    if (
        manifest["adapter_id"].startswith("simulator")
        or manifest["provider_id"].startswith("simulator")
        or manifest["environment_id"].startswith("production")
    ):
        raise ValueError("simulator or production environments are not qualification targets")
    for field, length in (
        ("source_commit", 40),
        ("adapter_code_sha256", 64),
        ("adapter_config_sha256", 64),
        ("provider_contract_sha256", 64),
        ("status_reader_sha256", 64),
        ("postcondition_reader_sha256", 64),
    ):
        if not _hex(manifest[field], length):
            raise ValueError("missing immutable source/contract digest")
    if manifest["status_reader_sha256"] == manifest["postcondition_reader_sha256"]:
        raise ValueError("status and postcondition observers must be separate")
    if manifest["fencing_contract"] not in (
        "ATOMIC_FENCE_AND_CAS", "PROVIDER_TRANSACTIONAL_REFUSAL",
    ):
        raise ValueError("provider-side atomic refusal contract required")
    retention, retry = (
        manifest["idempotency_retention_seconds"],
        manifest["max_retry_seconds"],
    )
    if (
        type(retention) is not int or not 3600 <= retention <= 2_592_000
        or type(retry) is not int or not 1 <= retry < retention
    ):
        raise ValueError("unbounded provider retention or retry window")
    rows = manifest["scenarios"]
    if not isinstance(rows, list) or len(rows) != len(SCENARIOS):
        raise ValueError("all required G2 failure scenarios must be present")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != SCENARIO_FIELDS:
            raise ValueError("unexpected provider scenario evidence")
        name = row["scenario"]
        if not isinstance(name, str) or name not in SCENARIOS or name in seen:
            raise ValueError("missing, duplicate or unsupported provider scenario")
        seen.add(name)
        result, effect_count, attempts = SCENARIOS[name]
        if (
            row["result"] != result
            or type(row["applied_effect_count"]) is not int
            or row["applied_effect_count"] != effect_count
            or type(row["dispatch_attempts"]) is not int
            or not attempts <= row["dispatch_attempts"] <= 16
            or type(row["status_queries"]) is not int
            or not 0 <= row["status_queries"] <= 16
            or type(row["oracle_queries"]) is not int
            or not 0 <= row["oracle_queries"] <= 16
            or not _hex(row["trace_sha256"], 64)
        ):
            raise ValueError("scenario does not demonstrate conservative state")
        # Uncertain sends are not retried blindly. Duplicate/competing
        # simulations intentionally use the same effect identity.
        if (
            name in {
                "lost-reply-after-commit", "status-outage", "oracle-outage",
                "partial-apply", "retention-gap", "status-recovery",
            }
            and row["dispatch_attempts"] != 1
        ):
            raise ValueError("unresolved effect was redispatched")
        if (
            name in {
                "lost-reply-after-commit", "status-outage",
                "retention-gap", "status-recovery",
            }
            and row["status_queries"] < 1
        ):
            raise ValueError("status recovery evidence was not recorded")
        if result == "VERIFIED" and row["oracle_queries"] < 1:
            raise ValueError("claimed recovery lacks independent postcondition")
    if seen != set(SCENARIOS):
        raise ValueError("incomplete G2 failure scenario evidence")


def _check_traces(manifest: dict, traces_dir: Path) -> None:
    if not traces_dir.is_dir() or traces_dir.is_symlink():
        raise ValueError("isolated trace directory is required")
    for row in manifest["scenarios"]:
        # The input never controls a filename: fixed scenario keys prevent
        # traversal, symlink redirection and accidental credential discovery.
        filename = traces_dir / (row["scenario"] + ".jsonl")
        if not filename.is_file() or filename.is_symlink():
            raise ValueError("missing or redirected raw provider trace")
        with filename.open("rb") as handle:
            raw = handle.read(MAX_TRACE_BYTES + 1)
        if not raw or len(raw) > MAX_TRACE_BYTES:
            raise ValueError("empty or oversized provider trace")
        if hashlib.sha256(raw).hexdigest() != row["trace_sha256"]:
            raise ValueError("provider trace does not match frozen digest")


def _check_reviews(
    manifest: dict, reviews: list[dict], trusted_keys: dict[str, bytes],
) -> dict[str, str]:
    if (
        not isinstance(reviews, list) or len(reviews) != len(ROLES)
        or not isinstance(trusted_keys, dict) or set(trusted_keys) != set(ROLES)
    ):
        raise ValueError("three distinct pinned review authorities required")
    by_role = {}
    for review in reviews:
        if not isinstance(review, dict) or set(review) != REVIEW_FIELDS:
            raise ValueError("invalid provider review schema")
        role = review["role"]
        if not isinstance(role, str) or role not in ROLES or role in by_role:
            raise ValueError("missing or duplicate provider authority")
        by_role[role] = review
    if set(by_role) != set(ROLES):
        raise ValueError("incomplete provider authority set")

    digest = canonical_digest(manifest)
    fingerprints = {}
    for role in ROLES:
        review = by_role[role]
        key = serialization.load_pem_public_key(trusted_keys[role])
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("provider reviewer key must be Ed25519")
        raw_key = key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprint = hashlib.sha256(raw_key).hexdigest()
        date = review["signed_at_utc"]
        if (
            review["manifest_sha256"] != digest
            or review["signer_key_sha256"] != fingerprint
            or review["review_action"] != ACTION
            or not isinstance(date, str)
            or not 20 <= len(date) <= 35
            or not date.endswith("Z")
        ):
            raise ValueError("review was not bound to the frozen G2 manifest")
        stamp = datetime.fromisoformat(date.replace("Z", "+00:00"))
        if stamp.utcoffset() != timedelta(0):
            raise ValueError("provider reviewer timestamp claim invalid")
        encoded = review["signature"]
        if not isinstance(encoded, str) or len(encoded) != 86:
            raise ValueError("invalid provider reviewer signature")
        signature = base64.b64decode(
            encoded + "==", altchars=b"-_", validate=True,
        )
        if (
            len(signature) != 64
            or base64.urlsafe_b64encode(signature).decode().rstrip("=") != encoded
        ):
            raise ValueError("noncanonical provider signature")
        key.verify(signature, review_bytes(review))
        fingerprints[role] = fingerprint
    if len(set(fingerprints.values())) != len(ROLES):
        raise ValueError("reused provider witness/reviewer/release signing key")
    return fingerprints


def verify_qualification(
    manifest: dict, traces_dir: Path, reviews: list[dict],
    trusted_keys: dict[str, bytes],
) -> dict:
    """Verify local G2 evidence, but never authorize provider effects."""
    try:
        _check_manifest(manifest)
        _check_traces(manifest, traces_dir)
        fingerprints = _check_reviews(manifest, reviews, trusted_keys)
    except (
        ValueError, TypeError, KeyError, AttributeError, OSError,
        InvalidSignature, binascii.Error, UnicodeError,
    ) as exc:
        raise ValueError("external adapter qualification evidence invalid") from exc
    return {
        "status": "EVIDENCE_INTEGRITY_VALID_ONLY",
        "manifest_sha256": canonical_digest(manifest),
        "adapter_id": manifest["adapter_id"],
        "provider_id": manifest["provider_id"],
        "scenario_count": len(SCENARIOS),
        "reviewer_key_fingerprints": fingerprints,
        "provider_execution_certified": False,
        "independent_review_certified": False,
        "provider_retention_certified": False,
        "trusted_timestamps_certified": False,
        "production_gate_passed": False,
        "external_effects_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline G2 evidence integrity check (never enables effects)"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--traces-dir", required=True)
    parser.add_argument("--reviews", required=True)
    parser.add_argument("--provider-key", required=True)
    parser.add_argument("--security-key", required=True)
    parser.add_argument("--release-owner-key", required=True)
    args = parser.parse_args()
    try:
        container = read_json(args.reviews)
        if not isinstance(container, dict) or set(container) != {"reviews"}:
            raise ValueError("invalid provider reviewer input")
        result = verify_qualification(
            read_json(args.manifest), Path(args.traces_dir),
            container["reviews"],
            {
                "provider_witness": load_trusted_public_key(args.provider_key),
                "security_reviewer": load_trusted_public_key(args.security_key),
                "release_owner": load_trusted_public_key(args.release_owner_key),
            },
        )
    except (ValueError, OSError, TypeError):
        result = {"status": "INVALID", "production_gate_passed": False,
                  "external_effects_enabled": False}
    print(json.dumps(result, sort_keys=True, indent=2))
    if result["status"] == "INVALID":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
