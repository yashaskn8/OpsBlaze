"""Synchronous query-time retrieval engine for Feature E4 with full-text search."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.lesson import LessonCitation, sanitize_lesson_summary_for_llm
from backend.observability.metrics import metrics
from backend.persistence.models import LessonUsage

logger = logging.getLogger("opsblaze.learning.retrieval")


class LessonRetriever:
    """Retrieves operational lessons with synchronous consent and outcome invalidation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def retrieve_lessons(
        self,
        tenant_id: uuid.UUID,
        query_text: str,
        limit: int = 3,
        max_tokens: int = 1500,
        incident_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        *,
        now: datetime | None = None,
    ) -> list[LessonCitation]:
        """Synchronously retrieves active, valid lessons matching query text.

        Enforces:
        1. Tenant consent epoch matching and reuse allowed.
        2. Source outcome revision settled and not superseded by any newer revision.
        3. Validity timestamps current.
        4. Read-only side-effect free execution (fails closed on error).
        5. Indirect prompt injection neutralization.
        6. Recording of usage lineage if incident_id is provided.
        """
        current_time = now or datetime.now(UTC)
        sanitized_query = query_text.strip()
        if not sanitized_query:
            return []

        # Synchronous query with websearch_to_tsquery and strict invalidation joins
        sql = text("""
            SELECT 
                l.id,
                l.lesson_id,
                l.version,
                l.lesson_type,
                l.title,
                l.redacted_summary,
                l.confidence_band,
                l.source_outcome_revision_id,
                r.rule_version
            FROM operational_lesson l
            JOIN consent_epoch e ON l.tenant_id = e.tenant_id
            JOIN outcome_revision r ON l.source_outcome_revision_id = r.id
            LEFT JOIN experience_capsule c ON l.source_capsule_id = c.id
            WHERE l.tenant_id = :tenant_id
              AND l.lifecycle_state = 'ACTIVE'
              AND l.valid_from <= :now
              AND l.valid_until > :now
              AND e.reuse_allowed = TRUE
              AND l.consent_epoch = e.current_epoch
              AND r.label IN ('VERIFIED_SUCCESS', 'VERIFIED_FAILURE')
              AND (l.source_capsule_id IS NULL OR c.lifecycle_state NOT IN ('TOMBSTONED', 'PURGED', 'QUARANTINED'))
              AND NOT EXISTS (
                  SELECT 1 FROM outcome_revision r_newer
                  WHERE r_newer.tenant_id = l.tenant_id
                    AND r_newer.incident_id = r.incident_id
                    AND r_newer.revision_no > r.revision_no
              )
              AND l.search_vector @@ websearch_to_tsquery('english', :query)
            ORDER BY ts_rank_cd(l.search_vector, websearch_to_tsquery('english', :query)) DESC
            LIMIT :limit
        """)

        citations: list[LessonCitation] = []
        token_estimate = 0

        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        sql,
                        {
                            "tenant_id": tenant_id,
                            "now": current_time,
                            "query": sanitized_query,
                            "limit": limit,
                        },
                    )
                ).all()

                for row in rows:
                    raw_summary = row.redacted_summary or ""
                    clean_summary = sanitize_lesson_summary_for_llm(raw_summary)

                    # Estimate token cost (~1 token per 4 chars)
                    approx_tokens = len(clean_summary) // 4 + len(row.title) // 4 + 20
                    if token_estimate + approx_tokens > max_tokens and citations:
                        break
                    token_estimate += approx_tokens

                    # Compute retrieval digest for attribution
                    ret_digest = hashlib.sha256(
                        f"{row.lesson_id}:{row.version}:{current_time.isoformat()}".encode()
                    ).hexdigest()

                    citation = LessonCitation(
                        lesson_id=row.lesson_id,
                        version=row.version,
                        lesson_type=row.lesson_type,
                        title=row.title,
                        sanitized_summary=clean_summary,
                        confidence_band=row.confidence_band,
                        source_outcome_revision_id=row.source_outcome_revision_id,
                        source_digest=f"rev:{row.source_outcome_revision_id}:{row.rule_version}",
                        retrieval_digest=ret_digest,
                    )
                    citations.append(citation)

                    # Record lineage if incident_id is available
                    if incident_id:
                        usage = LessonUsage(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            incident_id=incident_id,
                            step_id=step_id,
                            lesson_id=row.lesson_id,
                            lesson_version=row.version,
                            policy_bundle_version="v1",
                            retrieval_digest=ret_digest,
                            used_at=current_time,
                        )
                        session.add(usage)

                if incident_id and citations:
                    try:
                        await session.commit()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Failed to record lesson usage lineage: %s", e)
                        await session.rollback()

                metrics.inc("lesson_retrieval_hit", len(citations))
                return citations

        except Exception as e:  # noqa: BLE001
            # Critical invariant: read path fails closed without breaking incident investigation
            logger.error("Synchronous lesson retrieval encountered error, falling back to no-memory: %s", e)
            metrics.inc("lesson_no_memory_fallback")
            return []
