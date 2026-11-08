"""Deterministic redaction and payload normalization for OpsBlaze Feature E1."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from backend.domain.experience import CapsuleRedactionError

MAX_SUMMARY_BYTES = 4096
REDACTION_VERSION = "redact-v1"

# Compiled regex patterns for redaction
_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+")
_AWS_KEY_PATTERN = re.compile(r"(?i)\b(AKIA|ABIA|ACCA)[0-9A-Z]{16}\b")
_PASSWORD_PATTERN = re.compile(r"""(?i)(["']?(?:password|passwd|secret|api_key|token)["']?\s*[:=]\s*["']?)([^\s"',;]+)(["']?)""")
_DB_URL_PATTERN = re.compile(r"(?i)(postgres(?:ql)?(?:\+[a-z0-9]+)?://)([^:@/]+):([^@/]+)@")


class ExperienceRedactor:
    """Provides deterministic secret scrubbing, length bounding, and digest calculation."""

    @classmethod
    def redact_and_normalize(
        cls, raw_text: str | None, *, max_bytes: int = MAX_SUMMARY_BYTES
    ) -> tuple[str, str]:
        """Redact secrets, enforce byte limits, normalize Unicode, and compute digest.

        Returns
        -------
        tuple[str, str]
            Tuple of (redacted_summary_markdown, canonical_sha256_digest).
        """
        if raw_text is None:
            raw_text = ""

        # Validate string encoding (reject null bytes and non-unicode binary)
        if "\x00" in raw_text:
            raise CapsuleRedactionError("Payload contains null bytes (binary corruption)")

        try:
            # 1. Unicode normalization (NFKC)
            norm = unicodedata.normalize("NFKC", raw_text)
        except Exception as exc:
            raise CapsuleRedactionError(f"Unicode normalization failed: {exc}") from exc

        # 2. Secret Redaction
        redacted = _JWT_PATTERN.sub("[REDACTED:JWT]", norm)
        redacted = _BEARER_PATTERN.sub("Bearer [REDACTED:BEARER_TOKEN]", redacted)
        redacted = _AWS_KEY_PATTERN.sub("[REDACTED:AWS_KEY]", redacted)
        redacted = _PASSWORD_PATTERN.sub(r"\1[REDACTED:SECRET]\3", redacted)
        redacted = _DB_URL_PATTERN.sub(r"\1[REDACTED:USER]:[REDACTED:PASS]@", redacted)

        # 3. Byte Truncation
        encoded = redacted.encode("utf-8")
        if len(encoded) > max_bytes:
            suffix = "\n[TRUNCATED: MAX_BYTES_EXCEEDED]"
            suffix_bytes = suffix.encode("utf-8")
            cut_limit = max(0, max_bytes - len(suffix_bytes))
            truncated_bytes = encoded[:cut_limit]
            decoded_truncated = truncated_bytes.decode("utf-8", errors="ignore")
            redacted = decoded_truncated + suffix

        # 4. Canonical Digest
        final_bytes = redacted.encode("utf-8")
        digest = hashlib.sha256(final_bytes).hexdigest()

        return redacted, digest
