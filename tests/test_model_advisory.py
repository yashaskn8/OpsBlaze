
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.investigator.advisory import (
    MAX_OUTPUT_TOKENS, ReadOnlyAdvice, advise_after_unhealthy,
)
from backend.models import ModelAdvisory, ModelTokenWindow
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings


class FakeResponses:
    def __init__(self, choice=None, failure=None):
        self.choice = choice
        self.failure = failure
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        assert kwargs["max_output_tokens"] == MAX_OUTPUT_TOKENS
        assert kwargs["store"] is False
        assert kwargs["text_format"] is ReadOnlyAdvice
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(
            status="completed",
            output_parsed=self.choice,
            usage=SimpleNamespace(total_tokens=37),
        )


@pytest.mark.asyncio
async def test_optional_advisory_is_off_by_default(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t", "k", IncidentCreate(target_id="checkout"))
    assert await advise_after_unhealthy(sessions_fixture, "t", incident.id) is None
    async with sessions_fixture() as session:
        assert (await session.scalars(select(ModelAdvisory))).all() == []


@pytest.mark.asyncio
async def test_model_choice_is_scoped_cached_and_read_only(sessions_fixture, monkeypatch):
    monkeypatch.setattr(
        "backend.investigator.advisory.get_settings",
        lambda: Settings(ai_advisory_enabled=True, ai_advisory_model="test-model"),
    )
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t", "k", IncidentCreate(target_id="checkout"))
    fake = SimpleNamespace(responses=FakeResponses(ReadOnlyAdvice(
        next_probe="simulator.version", reason_code="NEED_VERSION",
    )))
    chosen = await advise_after_unhealthy(sessions_fixture, "t", incident.id, fake)
    assert chosen.next_probe == "simulator.version"
    assert chosen.tokens_used == 37
    assert chosen.tokens_reserved == 512
    assert await advise_after_unhealthy(sessions_fixture, "t", incident.id, fake) == chosen
    assert fake.responses.calls == 1
    assert await advise_after_unhealthy(sessions_fixture, "other", incident.id, fake) is None


@pytest.mark.asyncio
async def test_model_failure_has_no_blind_retries(sessions_fixture, monkeypatch):
    monkeypatch.setattr(
        "backend.investigator.advisory.get_settings",
        lambda: Settings(ai_advisory_enabled=True, ai_advisory_model="test-model"),
    )
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "t", "k", IncidentCreate(target_id="checkout"))
    fake = SimpleNamespace(responses=FakeResponses(failure=RuntimeError("service offline")))
    assert await advise_after_unhealthy(sessions_fixture, "t", incident.id, fake) is None
    assert await advise_after_unhealthy(sessions_fixture, "t", incident.id, fake) is None
    assert fake.responses.calls == 1
    async with sessions_fixture() as session:
        row = await session.get(ModelAdvisory, incident.id)
        assert row.state == "FAILED"


@pytest.mark.asyncio
async def test_tenant_budget_limits_actual_provider_calls(sessions_fixture, monkeypatch):
    monkeypatch.setattr(
        "backend.investigator.advisory.get_settings",
        lambda: Settings(
            ai_advisory_enabled=True, ai_advisory_model="test-model",
        ),
    )
    monkeypatch.setattr(
        "backend.budgets.get_settings",
        lambda: Settings(ai_token_limit_per_minute=512),
    )
    async with sessions_fixture() as session:
        first = await admit_incident(
            session, "t", "first", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        second = await admit_incident(
            session, "t", "second", IncidentCreate(target_id="billing")
        )
    fake = SimpleNamespace(responses=FakeResponses(ReadOnlyAdvice(
        next_probe="simulator.version", reason_code="NEED_VERSION",
    )))
    assert await advise_after_unhealthy(
        sessions_fixture, "t", first.id, fake
    ) is not None
    assert await advise_after_unhealthy(
        sessions_fixture, "t", second.id, fake
    ) is None
    assert fake.responses.calls == 1
    async with sessions_fixture() as session:
        window = await session.scalar(select(ModelTokenWindow))
        assert window.reserved_tokens == 512
        assert window.used_tokens == 37
        assert (await session.get(ModelAdvisory, second.id)) is None
