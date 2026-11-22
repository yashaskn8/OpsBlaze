"""Verify an external evaluator's frozen E5 preregistration.

Cryptographic integrity is necessary but cannot certify evaluator independence,
trusted time, oracle custody, or any production policy promotion.
"""

import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.evaluation.heldout import canonical_digest

SCHEMA = "opsblaze-e5-prereg-v1"
REQUIRED_FIELDS = frozenset({
    "schema", "manifest_sha256", "signer_public_key_sha256",
    "signed_at_utc", "expected_pairs", "primary_metric", "stopping_rule",
    "arm_versions", "signature",
})
PRIMARY_METRIC = "verified_recovery_per_total_cost"
STOPPING_RULE = "fixed_preregistered_sample"


def registration_bytes(registration: dict) -> bytes:
    """Domain-separate the exact typed claims from every other signature use."""
    body = {name: value for name, value in registration.items()
            if name != "signature"}
    return (
        b"OpsBlaze E5 frozen evaluation preregistration v1\n"
        + json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    )


def load_trusted_public_key(path: str) -> bytes:
    """Read a bounded *externally pinned* evaluator public key, never a secret."""
    with Path(path).open("rb") as handle:
        public_key = handle.read(4097)
    if not 0 < len(public_key) <= 4096:
        raise ValueError("trusted evaluator key is empty or oversized")
    return public_key


def verify_preregistration(
    manifest: dict, registration: dict, trusted_public_key_pem: bytes,
) -> dict:
    """Verify signature, key pin, immutable manifest and fixed trial count.

    The signed date is an authenticated *claim*, not a trusted timestamp.
    A caller must independently attest public-key custody and signing time.
    """
    try:
        if (
            not isinstance(manifest, dict)
            or not isinstance(registration, dict)
            or set(registration) != REQUIRED_FIELDS
            or registration["schema"] != SCHEMA
            or registration["primary_metric"] != PRIMARY_METRIC
            or registration["stopping_rule"] != STOPPING_RULE
            or type(registration["expected_pairs"]) is not int
            or not 32 <= registration["expected_pairs"] <= 10000
            or registration["expected_pairs"] < manifest["min_pairs"]
            or registration["manifest_sha256"] != canonical_digest(manifest)
        ):
            raise ValueError("invalid frozen preregistration fields")
        arm_versions = registration["arm_versions"]
        if (
            not isinstance(arm_versions, dict)
            or set(arm_versions) != {"candidate", "incumbent"}
            or any(not isinstance(value, str)
                   or not 1 <= len(value) <= 128
                   for value in arm_versions.values())
        ):
            raise ValueError("arm versions must be frozen and named")
        time_value = registration["signed_at_utc"]
        if (
            not isinstance(time_value, str)
            or not 20 <= len(time_value) <= 35
            or not time_value.endswith("Z")
        ):
            raise ValueError("malformed signing time claim")
        signed_at = datetime.fromisoformat(time_value.replace("Z", "+00:00"))
        if signed_at.utcoffset() != timedelta(0):
            raise ValueError("signing time claim must be UTC")

        public_key = serialization.load_pem_public_key(trusted_public_key_pem)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("evaluator signature key must be Ed25519")
        raw_key = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprint = hashlib.sha256(raw_key).hexdigest()
        if registration["signer_public_key_sha256"] != fingerprint:
            raise ValueError("registration signer differs from independently trusted key")

        signature_text = registration["signature"]
        if not isinstance(signature_text, str) or len(signature_text) != 86:
            raise ValueError("invalid detached Ed25519 signature encoding")
        signature = base64.b64decode(
            signature_text + "==", altchars=b"-_", validate=True,
        )
        if (
            len(signature) != 64
            or base64.urlsafe_b64encode(signature).decode().rstrip("=")
                != signature_text
        ):
            raise ValueError("noncanonical detached signature")
        public_key.verify(signature, registration_bytes(registration))
    except (
        ValueError, TypeError, KeyError, AttributeError, InvalidSignature,
        binascii.Error, UnicodeError,
    ) as exc:
        raise ValueError("signed preregistration cannot be trusted") from exc
    return {
        "status": "VALID_SIGNATURE",
        "manifest_sha256": registration["manifest_sha256"],
        "signer_public_key_sha256": fingerprint,
        "expected_pairs": registration["expected_pairs"],
        "independent_custody_certified": False,
        "trusted_timestamp_certified": False,
        "promotion_eligible": False,
    }
