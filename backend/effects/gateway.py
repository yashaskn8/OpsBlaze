"""B3: persisted receipt before call, no blind retry after uncertainty."""

from dataclasses import dataclass
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import (
    Permit, SimulatorAdapter, StaleFence, TargetVersionConflict, utc
)
from backend.models import ApprovalGrant, EffectReceipt, IncidentPlan, RevokedIdentity


class EffectConflict(Exception):
    pass


async def prepare_receipt(
    sessions: async_sessionmaker[AsyncSession],
    permit: Permit,
) -> UUID:
    # A deterministic effect identity survives Temporal retries and crashes.
    effect_id = uuid5(
        NAMESPACE_URL,
        f"opsblaze/{permit.tenant_id}/{permit.incident_id}/{permit.action_digest}",
    )
    key = str(effect_id)
    async with sessions() as session:
        async with session.begin():
            dialect = session.bind.dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise RuntimeError("unsupported effect receipt database")

            # SELECT FOR UPDATE cannot lock a row that does not exist.
            # Insert once and compare the original immutable intent under
            # the unique primary key; retries never create another receipt.
            await session.execute(
                insert(EffectReceipt).values(
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
                ).on_conflict_do_nothing(index_elements=["id"])
            )
            receipt = await session.scalar(
                select(EffectReceipt)
                .where(
                    EffectReceipt.tenant_id == permit.tenant_id,
                    EffectReceipt.idempotency_key == key,
                )
                .with_for_update()
            )
            if receipt is None or (
                receipt.adapter_id != "simulator"
                or receipt.incident_id != permit.incident_id
                or receipt.target_id != permit.target_id
                or receipt.plan_digest != permit.action_digest
                or receipt.fence != permit.fence
                or receipt.expected_version != permit.expected_version
            ):
                raise EffectConflict("effect identity reused for another plan or permit")
            return receipt.id

async def set_state(
    sessions: async_sessionmaker[AsyncSession],
    effect_id: UUID,
    state: str,
    observed_version: int | None = None,
    plan_id: UUID | None = None,
) -> None:
    allowed = {
        "PREPARED": {"SENT", "UNKNOWN", "NEEDS_REVIEW"},
        "SENT": {"UNKNOWN", "CONFIRMED", "FAILED", "NEEDS_REVIEW"},
        "UNKNOWN": {"CONFIRMED", "FAILED", "NEEDS_REVIEW"},
        "CONFIRMED": {"VERIFIED", "NEEDS_REVIEW"},
    }
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id, with_for_update=True)
            if receipt is None:
                raise ValueError("unknown receipt")
            if state == receipt.state:
                # SENT is a one-winner boundary: a second worker must query
                # by effect identity, not call the mutating adapter again.
                if state == "SENT":
                    raise EffectConflict("effect already dispatched")
                return
            if state not in allowed.get(receipt.state, set()):
                raise EffectConflict(f"invalid receipt transition {receipt.state} -> {state}")
            if state == "CONFIRMED" and (
                type(observed_version) is not int
                or observed_version != receipt.expected_version + 1
            ):
                raise EffectConflict("confirmed effect version is inconsistent")
            if state == "VERIFIED" and (
                type(receipt.observed_version) is not int
                or receipt.observed_version != receipt.expected_version + 1
            ):
                raise EffectConflict("cannot verify an inconsistent effect version")
            if state == "SENT" and receipt.adapter_id != "simulator":
                raise EffectConflict("non-simulator effect adapter is disabled")
            if state == "SENT" and plan_id is None:
                from backend.settings import get_settings

                if get_settings().environment == "production":
                    raise EffectConflict(
                        "production receipt dispatch requires an approved plan ID"
                    )
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
                    or await session.get(
                        RevokedIdentity,
                        (receipt.tenant_id, approval.reviewer_id),
                    ) is not None
                    or approval.decision != "APPROVE"
                    or approval.plan_digest != receipt.plan_digest
                    or approval.plan_digest != plan.plan_digest
                    or approval.scope != "SIMULATOR_MARK_HEALTHY"
                    or utc(approval.expires_at) <= datetime.now(timezone.utc)
                    or plan.status != "APPROVED"
                    or plan.version != 2
                    or plan.action_id != "SIMULATOR_MARK_HEALTHY"
                    or plan.target_id != receipt.target_id
                    or plan.expected_version != receipt.expected_version
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
        original_version = receipt.expected_version
        attempts = receipt.attempt_count

    if state in {"VERIFIED", "FAILED", "NEEDS_REVIEW"}:
        return state
    if state in {"SENT", "UNKNOWN", "CONFIRMED"} and attempts != 1:
        # PREPARED/UNKNOWN must never turn into VERIFIED without the durable
        # one-winner SENT transition. Invalid persisted send counts are
        # evidence for manual review, not authority to trust remote status.
        try:
            await set_state(sessions, effect_id, "NEEDS_REVIEW")
        except EffectConflict:
            return await reconcile_effect(sessions, adapter, effect_id)
        return "NEEDS_REVIEW"
    if state in {"SENT", "UNKNOWN", "PREPARED"}:
        try:
            observed_version = await adapter.query_effect(effect_id)
        except (TimeoutError, ConnectionError):
            # A never-sent receipt has no uncertain outbound side effect.
            if state == "PREPARED":
                return "PREPARED"
            # Lookup failure is not proof of non-application. No blind retry.
            if state != "UNKNOWN":
                try:
                    await set_state(sessions, effect_id, "UNKNOWN")
                except EffectConflict:
                    return await reconcile_effect(sessions, adapter, effect_id)
            return "UNKNOWN"
        if observed_version is None:
            if state == "PREPARED":
                return "PREPARED"
            if state != "UNKNOWN":
                try:
                    await set_state(sessions, effect_id, "UNKNOWN")
                except EffectConflict:
                    # Another worker advanced the receipt after our stale read.
                    # Never replace CONFIRMED/VERIFIED with UNKNOWN.
                    return await reconcile_effect(sessions, adapter, effect_id)
            return "UNKNOWN"
        if state == "PREPARED":
            # No send was authorized. A reported remote effect is contradictory,
            # not permission to promote PREPARED -> CONFIRMED or recurse forever.
            try:
                await set_state(sessions, effect_id, "NEEDS_REVIEW")
            except EffectConflict:
                # Another worker won the dispatch race. Read its new state.
                return await reconcile_effect(sessions, adapter, effect_id)
            return "NEEDS_REVIEW"
        if (
            type(observed_version) is not int
            or observed_version != original_version + 1
        ):
            try:
                await set_state(sessions, effect_id, "NEEDS_REVIEW")
            except EffectConflict:
                return await reconcile_effect(sessions, adapter, effect_id)
            return "NEEDS_REVIEW"
        if state != "CONFIRMED":
            try:
                await set_state(sessions, effect_id, "CONFIRMED", observed_version)
            except EffectConflict:
                return await reconcile_effect(sessions, adapter, effect_id)
        expected_observed = observed_version

    if (
        expected_observed is None
        or type(expected_observed) is not int
        or expected_observed != original_version + 1
    ):
        try:
            await set_state(sessions, effect_id, "NEEDS_REVIEW")
        except EffectConflict:
            return await reconcile_effect(sessions, adapter, effect_id)
        return "NEEDS_REVIEW"

    try:
        witness = await adapter.observe(tenant_id, target_id)
    except (TimeoutError, ConnectionError):
        # Retain CONFIRMED while the independent oracle is unreachable:
        # no success claim and no replay of an already-applied effect.
        return "UNKNOWN"
    verified = witness is not None and witness == (expected_observed, True)
    terminal = "VERIFIED" if verified else "NEEDS_REVIEW"
    try:
        await set_state(sessions, effect_id, terminal)
    except EffectConflict:
        # A newer terminal record takes precedence over this observation.
        return await reconcile_effect(sessions, adapter, effect_id)
    return terminal


async def execute_simulator_effect(
    sessions: async_sessionmaker[AsyncSession],
    adapter: SimulatorAdapter,
    permit: Permit,
    plan_id: UUID | None = None,
) -> str:
    """Only explicitly reviewed plans can dispatch outside development."""
    if plan_id is None:
        from backend.settings import get_settings

        if get_settings().environment == "production":
            raise EffectConflict("production execution requires an approved plan ID")
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        state = receipt.state if receipt else None
    if state != "PREPARED":
        return await reconcile_effect(sessions, adapter, effect_id)

    try:
        await set_state(sessions, effect_id, "SENT", plan_id=plan_id)
    except EffectConflict:
        async with sessions() as session:
            current = await session.get(EffectReceipt, effect_id)
            state = current.state if current else None
        if state == "PREPARED":
            # Approval/fence refusal before the dispatch boundary.
            try:
                await set_state(sessions, effect_id, "NEEDS_REVIEW")
            except EffectConflict:
                # A competing worker may have dispatched in the meantime.
                return await reconcile_effect(sessions, adapter, effect_id)
            return "NEEDS_REVIEW"
        # Another worker already sent or completed the exact effect.
        # Status-only reconciliation is safe even after a lost reply.
        return await reconcile_effect(sessions, adapter, effect_id)
    try:
        observed_version = await adapter.apply(effect_id, permit)
    except (TimeoutError, ConnectionError):
        await set_state(sessions, effect_id, "UNKNOWN")
        return "UNKNOWN"
    except (StaleFence, TargetVersionConflict):
        await set_state(sessions, effect_id, "FAILED")
        return "FAILED"

    try:
        await set_state(sessions, effect_id, "CONFIRMED", observed_version)
    except EffectConflict:
        # A concurrent status-only reconciler may already have verified the
        # same effect after it was applied. Never retry apply.
        return await reconcile_effect(sessions, adapter, effect_id)
    return await reconcile_effect(sessions, adapter, effect_id)
