"""G2 offline external-adapter qualification evidence verifier.

Accepts local trace files and externally pinned reviewer keys. This does not
call a provider, trust witness independence, or grant any effect capability.
"""

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

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
TRACE_SCHEMA = "opsblaze-g2-trace-v1"
TRACE_FIELDS = frozenset({
    "schema", "scenario", "sequence", "effect_id",
    "permit_sha256", "event", "observed",
})
OBSERVATIONS = {
    "DISPATCH": frozenset({"SENT"}),
    "APPLIED": frozenset({"COMMITTED"}),
    "STATUS": frozenset({"FOUND", "MISSING", "UNAVAILABLE", "CONFLICT"}),
    "ORACLE": frozenset({"HEALTHY", "UNHEALTHY", "UNAVAILABLE"}),
}
EVENT_COUNT_FIELDS = {
    "DISPATCH": "dispatch_attempts",
    "APPLIED": "applied_effect_count",
    "STATUS": "status_queries",
    "ORACLE": "oracle_queries",
}
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
    """Open trace files without symlink or hard-link check/open races.

    The directory is opened once; all file descriptors resolve relative
    to it rather than re-resolving a possibly replaced path.
    """
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise ValueError("secure trace review requires native Linux")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory_fd = os.open(traces_dir, flags | os.O_DIRECTORY)
    try:
        identities = {}
        for row in manifest["scenarios"]:
            # Scenario names are validated against a static allowlist.
            name = row["scenario"] + ".jsonl"
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            with os.fdopen(file_fd, "rb") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("trace must be an unshared regular file")
                raw = handle.read(MAX_TRACE_BYTES + 1)
            if not raw or len(raw) > MAX_TRACE_BYTES:
                raise ValueError("empty or oversized provider trace")
            if hashlib.sha256(raw).hexdigest() != row["trace_sha256"]:
                raise ValueError("provider trace does not match frozen digest")
            identities[row["scenario"]] = _check_trace_events(row, raw)
        if identities["status-outage"] != identities["status-recovery"]:
            raise ValueError("recovery trace refers to a different effect or permit")
    finally:
        os.close(directory_fd)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate provider trace key")
        result[name] = value
    return result


def _bad_constant(_text: str) -> None:
    raise ValueError("non-finite provider trace number")


def _check_trace_events(summary: dict, raw: bytes) -> tuple[str, str]:
    """Check source events, not just a digest of an arbitrary text file.

    The reviewed provider must export a redacted JSONL record for every
    dispatch, committed effect, status lookup and independent witness read.
    This checks internal consistency, not whether the provider is truthful.
    """
    lines = raw.splitlines()
    expected_count = sum(summary[field] for field in EVENT_COUNT_FIELDS.values())
    if not 1 <= len(lines) == expected_count <= 64:
        raise ValueError("trace event count differs from scenario claims")
    reference = None
    counts = {kind: 0 for kind in OBSERVATIONS}
    statuses: list[str] = []
    oracle: list[str] = []
    last_phase = -1
    phase_order = {"DISPATCH": 0, "APPLIED": 1, "STATUS": 2, "ORACLE": 3}
    for sequence, line in enumerate(lines, 1):
        if not line or len(line) > 4096:
            raise ValueError("provider event line empty or oversized")
        try:
            event = json.loads(
                line, object_pairs_hook=_no_duplicate_keys,
                parse_constant=_bad_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider event is not strict JSONL") from exc
        if not isinstance(event, dict) or set(event) != TRACE_FIELDS:
            raise ValueError("unrecognized or potentially sensitive trace fields")
        kind = event["event"]
        observed = event["observed"]
        if (
            event["schema"] != TRACE_SCHEMA
            or event["scenario"] != summary["scenario"]
            or type(event["sequence"]) is not int
            or event["sequence"] != sequence
            or not isinstance(kind, str)
            or kind not in OBSERVATIONS
            or not isinstance(observed, str)
            or observed not in OBSERVATIONS[kind]
            or not _hex(event["permit_sha256"], 64)
            or not isinstance(event["effect_id"], str)
        ):
            raise ValueError("provider event does not match scenario contract")
        try:
            effect = UUID(event["effect_id"])
        except (ValueError, AttributeError) as exc:
            raise ValueError("malformed provider effect identity") from exc
        if str(effect) != event["effect_id"]:
            raise ValueError("noncanonical provider effect ID")
        phase = phase_order[kind]
        if phase < last_phase:
            raise ValueError("provider trace events are out of causal order")
        last_phase = phase
        identity = (event["effect_id"], event["permit_sha256"])
        if reference is None:
            reference = identity
        elif identity != reference:
            raise ValueError("changed effect identity or original permit")
        counts[kind] += 1
        if kind == "STATUS":
            statuses.append(observed)
        elif kind == "ORACLE":
            oracle.append(observed)

    if any(
        counts[kind] != summary[field]
        for kind, field in EVENT_COUNT_FIELDS.items()
    ):
        raise ValueError("provider trace contradicts recorded attempt counts")
    result = summary["result"]
    verified = (
        bool(statuses and oracle)
        and statuses[-1] == "FOUND"
        and oracle[-1] == "HEALTHY"
    )
    if result == "VERIFIED" and not verified:
        raise ValueError("verified effect lacks observed independent recovery")
    if result == "REJECTED" and (
        counts["APPLIED"] or "FOUND" in statuses or "HEALTHY" in oracle
    ):
        raise ValueError("rejected effect has contradictory commit evidence")
    if result == "UNKNOWN" and verified:
        raise ValueError("unknown effect incorrectly recorded as resolved")
    if result == "NEEDS_REVIEW" and (
        "CONFLICT" not in statuses and "UNHEALTHY" not in oracle
    ):
        raise ValueError("manual-review effect lacks anomaly evidence")
    name = summary["scenario"]
    if (
        (name == "status-outage" and "UNAVAILABLE" not in statuses)
        or (name == "retention-gap" and "MISSING" not in statuses)
        or (name == "oracle-outage" and "UNAVAILABLE" not in oracle)
        or (name == "partial-apply" and (
            "CONFLICT" not in statuses and "UNHEALTHY" not in oracle
        ))
    ):
        raise ValueError("failure scenario lacks its named fault observation")
    return reference


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
