"""B3: persisted receipt before call, no blind retry after uncertainty."""

from dataclasses import dataclass
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import (
    Permit, SimulatorAdapter, StaleFence, TargetVersionConflict
)
from backend.models import ApprovalGrant, EffectReceipt, IncidentPlan


class EffectConflict(Exception):
    pass


async def prepare_receipt(
    sessions: async_sessionmaker[AsyncSession],
    permit: Permit,
) -> UUID:
    # The effect identity survives retry and process loss.
    effect_id = uuid5(
        NAMESPACE_URL,
        f"opsblaze/{permit.tenant_id}/{permit.incident_id}/{permit.action_digest}",
    )
    key = str(effect_id)
    async with sessions() as session:
        async with session.begin():
            receipt = await session.scalar(
                select(EffectReceipt)
                .where(
                    EffectReceipt.tenant_id == permit.tenant_id,
                    EffectReceipt.idempotency_key == key,
                ).with_for_update()
            )
            if receipt:
                if (
                    receipt.incident_id != permit.incident_id
                    or receipt.target_id != permit.target_id
                    or receipt.plan_digest != permit.action_digest
                    or receipt.fence != permit.fence
                    or receipt.expected_version != permit.expected_version
                ):
                    raise EffectConflict("effect identity reused for another plan or permit")
                return receipt.id
            session.add(
                EffectReceipt(
                    id=effect_id,
                    tenant_id=permit.tenant_id,
                    incident_id=permit.incident_id,
                    plan_digest=permit.action_digest,
                    target_id=permit.target_id,
                    adapter_id="simulator",
                    idempotency_key=key,
                    fence=permit.fence,
                    expected_version=permit.expected_version,
                    state="PREPARED",
                    attempt_count=0,
                )
            )
    return effect_id


async def set_state(
    sessions: async_sessionmaker[AsyncSession],
    effect_id: UUID,
    state: str,
    observed_version: int | None = None,
    plan_id: UUID | None = None,
) -> None:
    allowed = {
        "PREPARED": {"SENT", "UNKNOWN", "NEEDS_REVIEW"},
        "SENT": {"UNKNOWN", "CONFIRMED", "FAILED"},
        "UNKNOWN": {"CONFIRMED", "FAILED", "NEEDS_REVIEW"},
        "CONFIRMED": {"VERIFIED", "NEEDS_REVIEW"},
    }
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id, with_for_update=True)
            if receipt is None:
                raise ValueError("unknown receipt")
            if state == receipt.state:
                return
            if state not in allowed.get(receipt.state, set()):
                raise EffectConflict(f"invalid receipt transition {receipt.state} -> {state}")
            if state == "SENT" and plan_id is not None:
                from datetime import datetime, timezone
                from backend.plans import current_plan_evidence_is_intact
                from backend.settings import get_settings

                approval = await session.scalar(
                    select(ApprovalGrant).where(
                        ApprovalGrant.plan_id == plan_id,
                        ApprovalGrant.tenant_id == receipt.tenant_id,
                    ).with_for_update()
                )
                plan = await session.scalar(
                    select(IncidentPlan).where(
                        IncidentPlan.id == plan_id,
                        IncidentPlan.tenant_id == receipt.tenant_id,
                        IncidentPlan.incident_id == receipt.incident_id,
                    )
                )
                if (
                    approval is None or plan is None
                    or approval.state != "APPROVED"
                    or approval.plan_digest != receipt.plan_digest
                    or approval.scope != "SIMULATOR_MARK_HEALTHY"
                    or approval.expires_at <= datetime.now(timezone.utc)
                    or plan.status != "APPROVED"
                    or plan.review_epoch != get_settings().review_epoch
                    or not await current_plan_evidence_is_intact(session, plan)
                ):
                    raise EffectConflict("approval expired, revoked or evidence changed")
                # Approval and SENT cross the dispatch boundary atomically.
                approval.state = "CONSUMED"
                approval.consumed_at = datetime.now(timezone.utc)
                approval.version += 1
            receipt.state = state
            if observed_version is not None:
                receipt.observed_version = observed_version
            if state == "SENT":
                receipt.attempt_count += 1


async def reconcile_effect(
    sessions: async_sessionmaker[AsyncSession],
    adapter: SimulatorAdapter,
    effect_id: UUID,
) -> str:
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        if receipt is None:
            raise ValueError("unknown receipt")
        state = receipt.state
        tenant_id, target_id = receipt.tenant_id, receipt.target_id
        expected_observed = receipt.observed_version

    if state in {"VERIFIED", "FAILED", "NEEDS_REVIEW"}:
        return state
    if state in {"SENT", "UNKNOWN", "PREPARED"}:
        observed_version = await adapter.query_effect(effect_id)
        if observed_version is None:
            if state != "UNKNOWN":
                await set_state(sessions, effect_id, "UNKNOWN")
            return "UNKNOWN"
        if state != "CONFIRMED":
            await set_state(sessions, effect_id, "CONFIRMED", observed_version)
        expected_observed = observed_version

    witness = await adapter.observe(tenant_id, target_id)
    verified = witness is not None and witness == (expected_observed, True)
    terminal = "VERIFIED" if verified else "NEEDS_REVIEW"
    await set_state(sessions, effect_id, terminal)
    return terminal


async def execute_simulator_effect(
    sessions: async_sessionmaker[AsyncSession],
    adapter: SimulatorAdapter,
    permit: Permit,
    plan_id: UUID | None = None,
) -> str:
    """The API path passes plan_id; direct use is reserved for simulator tests."""
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        state = receipt.state if receipt else None
    if state != "PREPARED":
        return await reconcile_effect(sessions, adapter, effect_id)

    try:
        await set_state(sessions, effect_id, "SENT", plan_id=plan_id)
    except EffectConflict:
        await set_state(sessions, effect_id, "NEEDS_REVIEW")
        return "NEEDS_REVIEW"
    try:
        observed_version = await adapter.apply(effect_id, permit)
    except (TimeoutError, ConnectionError):
        await set_state(sessions, effect_id, "UNKNOWN")
        return "UNKNOWN"
    except (StaleFence, TargetVersionConflict):
        await set_state(sessions, effect_id, "FAILED")
        return "FAILED"

    await set_state(sessions, effect_id, "CONFIRMED", observed_version)
    return await reconcile_effect(sessions, adapter, effect_id)
