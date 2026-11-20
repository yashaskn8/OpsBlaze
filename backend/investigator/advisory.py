
"""Optional dev-only structured model advisory; never authorizes any effect.

An incident gets at most one reserved model request. On a crash or ambiguous
provider response, deterministic investigation continues without model output.
"""

from dataclasses import dataclass
import logging
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import ModelAdvisory
from backend.settings import get_settings

log = logging.getLogger(__name__)
MAX_OUTPUT_TOKENS = 128


class ReadOnlyAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_probe: Literal["simulator.version", "ABSTAIN"]
    reason_code: Literal["NEED_VERSION", "INSUFFICIENT_EVIDENCE"]


@dataclass(frozen=True)
class AdvisoryResult:
    next_probe: str
    model_id: str
    tokens_used: int


async def advise_after_unhealthy(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    client=None,
) -> AdvisoryResult | None:
    settings = get_settings()
    if settings.environment != "development" or not settings.ai_advisory_enabled:
        return None

    async with sessions() as session:
        async with session.begin():
            row = await session.get(ModelAdvisory, incident_id, with_for_update=True)
            if row is not None:
                if row.tenant_id != tenant_id:
                    return None
                if row.state == "COMPLETE" and row.next_probe in {
                    "simulator.version", "ABSTAIN"
                }:
                    return AdvisoryResult(row.next_probe, row.model_id, row.tokens_used)
                # Previous unknown/in-flight/failed call never triggers another spend.
                return None
            session.add(ModelAdvisory(
                incident_id=incident_id, tenant_id=tenant_id,
                model_id=settings.ai_advisory_model, state="RESERVED",
                next_probe=None, reason_code=None,
                tokens_reserved=MAX_OUTPUT_TOKENS, tokens_used=0,
            ))

    advice = None
    used = 0
    try:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(max_retries=0, timeout=10.0)
        response = await client.responses.parse(
            model=settings.ai_advisory_model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Choose only a read-only simulator version observation "
                        "or abstain. Never propose code, credentials, URLs, "
                        "mutating actions, or permissions. Return the typed schema."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "The local simulator health probe returned healthy=false. "
                        "The only allowed follow-up is simulator.version. "
                        "If insufficient evidence, abstain."
                    ),
                },
            ],
            text_format=ReadOnlyAdvice,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            store=False,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise ValueError("model did not complete a structured response")
        candidate = ReadOnlyAdvice.model_validate(response.output_parsed)
        if (
            candidate.next_probe == "simulator.version"
            and candidate.reason_code != "NEED_VERSION"
        ) or (
            candidate.next_probe == "ABSTAIN"
            and candidate.reason_code != "INSUFFICIENT_EVIDENCE"
        ):
            raise ValueError("inconsistent advisory choice")
        advice = candidate
        usage = getattr(response, "usage", None)
        used = min(int(getattr(usage, "total_tokens", 0) or 0), 10000)
    except Exception:
        # No provider payload, raw prompt, or credential is logged.
        log.warning("AI advisory unavailable; using deterministic fallback")

    async with sessions() as session:
        async with session.begin():
            row = await session.get(ModelAdvisory, incident_id, with_for_update=True)
            if row is None or row.tenant_id != tenant_id or row.state != "RESERVED":
                return None
            row.state = "COMPLETE" if advice is not None else "FAILED"
            row.tokens_used = used
            if advice is not None:
                row.next_probe = advice.next_probe
                row.reason_code = advice.reason_code

    if advice is None:
        return None
    return AdvisoryResult(advice.next_probe, settings.ai_advisory_model, used)
