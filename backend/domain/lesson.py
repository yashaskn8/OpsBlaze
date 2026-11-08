"""Domain models, contracts, and enums for Feature E4 — Operational Lessons."""

from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LessonType(str, Enum):
    """Classification of the operational lesson."""

    VERIFIED_RECOVERY = "VERIFIED_RECOVERY"
    FAILURE_PATTERN = "FAILURE_PATTERN"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"


class LessonNotFoundError(ValueError):
    """Raised when an operational lesson cannot be found for tenant."""


class LessonConflictError(ValueError):
    """Raised on version conflict or concurrent modification of lesson."""


class LessonValidationError(ValueError):
    """Raised when lesson fails validation rules."""


class LessonLifecycle(str, Enum):
    """Guarded lifecycle states for operational lessons."""

    DRAFT = "DRAFT"
    SOURCE_CHECKED = "SOURCE_CHECKED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    QUARANTINED = "QUARANTINED"
    REVOKED = "REVOKED"


class LessonConfidenceBand(str, Enum):
    """Confidence levels backed by source evidence."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class LessonApplicabilityPredicate(BaseModel):
    """Structured predicate defining when a lesson applies."""

    model_config = ConfigDict(extra="ignore")

    target_subsystem: str = "general"
    failure_codes: list[str] = Field(default_factory=list)
    preconditions: dict[str, Any] = Field(default_factory=dict)
    environment: str = "production"


class LessonCitation(BaseModel):
    """Immutable citation metadata for a retrieved operational lesson."""

    lesson_id: uuid.UUID
    version: int
    lesson_type: str
    title: str
    sanitized_summary: str
    confidence_band: str
    source_outcome_revision_id: uuid.UUID
    source_digest: str
    retrieval_digest: str

    def to_prompt_block(self) -> str:
        """Format citation as an immutable XML/block representation for safe LLM ingestion."""
        return (
            f"<operational_lesson id=\"{self.lesson_id}\" v=\"{self.version}\" type=\"{self.lesson_type}\">\n"
            f"  <title>{self.title}</title>\n"
            f"  <summary>{self.sanitized_summary}</summary>\n"
            f"  <source digest=\"{self.source_digest}\" />\n"
            f"</operational_lesson>"
        )


def sanitize_lesson_summary_for_llm(raw_summary: str, max_words: int = 250) -> str:
    """Sanitize lesson text to neutralize indirect prompt injection attempts.

    Strips markdown code blocks, backticks, HTML tags, role prefixes, and suspicious instruction phrases.
    """
    cleaned = raw_summary.replace("```", " ").replace("`", " ")
    # Strip HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Strip role prefixes used in conversational injection attacks
    cleaned = re.sub(r"\b(Human|Assistant|System):\s*", " ", cleaned, flags=re.IGNORECASE)
    # Neutralize prompt injection phrases
    cleaned = re.sub(
        r"(ignore (previous|all) instructions|system prompt|override policy|bypass approval)",
        "[REDACTED_INSTRUCTION]",
        cleaned,
        flags=re.IGNORECASE,
    )
    words = cleaned.split()
    if len(words) > max_words:
        words = words[:max_words] + ["[TRUNCATED]"]
    return " ".join(words).strip()
