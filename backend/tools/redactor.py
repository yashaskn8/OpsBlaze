"""Secret redaction layer for tool outputs and diagnostic data."""

from __future__ import annotations

import re
from typing import Any

# Regex patterns for common secrets
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key
    re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE),  # Bearer Token
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"),  # PEM keys
    re.compile(r"(password|passwd|secret|token|apikey|api_key)\s*[:=]\s*['\"]?([^'\"\s,;]+)['\"]?", re.IGNORECASE),
    re.compile(r"postgres(?:ql)?://([^:]+):([^@]+)@", re.IGNORECASE),  # DB URI credentials
]

REDACTED_PLACEHOLDER = "[REDACTED_SECRET]"


class SecretRedactor:
    """Detects and masks credentials and secrets in unstructured or structured text."""

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        result = text
        for pattern in _SECRET_PATTERNS:
            # For key-value patterns, preserve the key name
            if "password" in pattern.pattern:
                result = pattern.sub(r"\1: " + REDACTED_PLACEHOLDER, result)
            elif "postgres" in pattern.pattern:
                result = pattern.sub(r"postgres://\1:" + REDACTED_PLACEHOLDER + "@", result)
            else:
                result = pattern.sub(REDACTED_PLACEHOLDER, result)
        return result

    def redact_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact dictionary values."""
        redacted: dict[str, Any] = {}
        for k, v in data.items():
            # If the key itself indicates a secret, mask directly
            if any(s in k.lower() for s in ("password", "passwd", "secret", "token", "apikey", "api_key", "aws_key")):
                redacted[k] = REDACTED_PLACEHOLDER
            elif isinstance(v, str):
                redacted[k] = self.redact_text(v)
            elif isinstance(v, dict):
                redacted[k] = self.redact_dict(v)
            elif isinstance(v, list):
                redacted[k] = [
                    self.redact_dict(item) if isinstance(item, dict)
                    else (self.redact_text(item) if isinstance(item, str) else item)
                    for item in v
                ]
            else:
                redacted[k] = v
        return redacted
