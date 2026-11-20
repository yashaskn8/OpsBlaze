
"""E8 executable conformance smoke on disposable simulator fixtures only.

A LAB_PASS report is not production enablement or external adapter certification.
"""

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import (
    EffectIdentityConflict, Permit, SimulatorAdapter, StaleFence,
    TargetVersionConflict,
)
from backend.effects.gateway import (
    execute_simulator_effect, reconcile_effect, prepare_receipt,
)
from backend.effects.reservations import reserve_simulator_target
from backend.models import (
    AdapterConformanceRun, AdmissionUsage, EffectReceipt, Incident, OutboxEntry,
    ResourceEpoch, ResourceReservation, SimulatorAppliedEffect, SimulatorTarget,
    UIEvent, PolicyAssignment,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings

SUITE_VERSION = "simulator-conformance-v4"
RETENTION_SECONDS = 3600
REQUIRED_SCENARIOS = frozenset({
    "first-apply", "duplicate-apply", "effect-status", "stale-fence",
    "version-cas", "lost-ack-lookup", "expired-lease", "missing-effect",
    "effect-identity-binding", "status-store-outage", "status-store-recovered",
    "oracle-outage", "oracle-recovered", "retention-gap",
    "retention-recovered", "external-version-edit",
})


def complete_report(results: list[dict]) -> bool:
    names = [row.get("scenario") for row in results]
    return (
        len(names) == len(REQUIRED_SCENARIOS)
        and set(names) == REQUIRED_SCENARIOS
        and all(row.get("passed") is True for row in results)
    )


def simulator_digest() -> str:
    return hashlib.sha256(
        Path(__file__).with_name("simulator.py").read_bytes()
    ).hexdigest()


async def execute_suite(
    sessions: async_sessionmaker[AsyncSession],
) -> AdapterConformanceRun:
    if get_settings().environment != "development":
        raise RuntimeError("conformance fixture requires development mode")

    tenant = "conform-" + uuid4().hex[:16]
    target_id = "fixture"
    results: list[dict] = []
    adapter = SimulatorAdapter(sessions)

    def outcome(name: str, passed: bool, note: str) -> None:
        results.append({"scenario": name, "passed": passed, "note": note[:160]})

    async with sessions() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id=tenant, target_id=target_id,
                    version=1, healthy=False, last_fence=0,
                )
            )
            session.add(
                ResourceEpoch(
                    tenant_id=tenant, target_id=target_id,
                    latest_fence=0, version_seen=1,
                )
            )

    incident_id: UUID | None = None
    try:
        async with sessions() as session:
            incident = await admit_incident(
                session, tenant, "conformance",
                IncidentCreate(target_id=target_id),
            )
            incident_id = incident.id
        permit = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "a" * 64,
        )
        effect_one = uuid4()
        version = await adapter.apply(effect_one, permit)
        outcome("first-apply", version == 2, "single isolated simulator version change")

        duplicate = await adapter.apply(effect_one, permit)
        outcome(
            "duplicate-apply",
            duplicate == version and await adapter.observe(tenant, target_id) == (2, True),
            "same effect identity cannot increment version twice",
        )
        outcome(
            "effect-status",
            await adapter.query_effect(effect_one) == version,
            "lookup uses stable effect identity",
        )
        try:
            changed_permit = Permit(
                tenant_id=permit.tenant_id,
                target_id=permit.target_id,
                incident_id=permit.incident_id,
                action_digest="9" * 64,
                fence=permit.fence,
                expected_version=permit.expected_version,
                expires_at=permit.expires_at,
            )
            await adapter.apply(effect_one, changed_permit)
            outcome("effect-identity-binding", False, "permit mismatch was accepted")
        except EffectIdentityConflict:
            outcome(
                "effect-identity-binding", True,
                "effect ID cannot be reused with another action digest",
            )


        try:
            await adapter.apply(uuid4(), permit)
            outcome("stale-fence", False, "old released fence was accepted")
        except StaleFence:
            outcome("stale-fence", True, "old fence rejected on new effect identity")

        permit_two = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "b" * 64,
        )
        wrong_version = Permit(
            tenant_id=permit_two.tenant_id,
            target_id=permit_two.target_id,
            incident_id=permit_two.incident_id,
            action_digest=permit_two.action_digest,
            fence=permit_two.fence,
            expected_version=permit_two.expected_version + 1,
            expires_at=permit_two.expires_at,
        )
        try:
            await adapter.apply(uuid4(), wrong_version)
            outcome("version-cas", False, "mismatched target version was applied")
        except TargetVersionConflict:
            outcome("version-cas", True, "mismatched version rejected")

        effect_two = uuid4()
        await adapter.apply(effect_two, permit_two)
        try:
            raise TimeoutError("simulated response lost after commit")
        except TimeoutError:
            lookup = await adapter.query_effect(effect_two)
        outcome(
            "lost-ack-lookup",
            lookup == 3 and await adapter.apply(effect_two, permit_two) == 3,
            "lookup resolves lost response without duplicate apply",
        )

        permit_three = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "c" * 64,
        )
        async with sessions() as session:
            async with session.begin():
                reservation = await session.get(
                    ResourceReservation, (tenant, target_id),
                )
                reservation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        try:
            await adapter.apply(uuid4(), permit_three)
            outcome("expired-lease", False, "expired reservation was accepted")
        except StaleFence:
            outcome("expired-lease", True, "expired reservation rejected")

        outcome(
            "missing-effect",
            await adapter.query_effect(uuid4()) is None,
            "unknown effect lookup returns no fabricated success",
        )

        # A lost reply after commit followed by a status-store outage must
        # remain unresolved. The original effect identity is never reapplied.
        permit_four = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "d" * 64,
        )

        class LostAckAndStatusOutage(SimulatorAdapter):
            async def apply(self, effect_id, permit):
                await super().apply(effect_id, permit)
                raise TimeoutError("acknowledgment lost after commit")

            async def query_effect(self, effect_id):
                raise ConnectionError("status store is unavailable")

        outage = LostAckAndStatusOutage(sessions)
        effect_four = await prepare_receipt(sessions, permit_four)
        pending = await execute_simulator_effect(sessions, outage, permit_four)
        still_pending = await reconcile_effect(sessions, outage, effect_four)
        outcome(
            "status-store-outage",
            pending == still_pending == "UNKNOWN"
            and await adapter.observe(tenant, target_id) == (4, True),
            "unknown receipt never triggers a second apply after lookup failure",
        )
        recovered = await reconcile_effect(sessions, adapter, effect_four)
        outcome(
            "status-store-recovered",
            recovered == "VERIFIED"
            and await adapter.query_effect(effect_four) == 4,
            "same receipt resolves from original effect identity",
        )

        # An unavailable postcondition oracle cannot upgrade CONFIRMED to
        # VERIFIED until it independently observes the target again.
        permit_five = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "e" * 64,
        )

        class PostconditionOutage(SimulatorAdapter):
            async def observe(self, tenant_id, target_id):
                raise TimeoutError("independent postcondition unavailable")

        oracle_outage = PostconditionOutage(sessions)
        effect_five = await prepare_receipt(sessions, permit_five)
        unresolved = await execute_simulator_effect(
            sessions, oracle_outage, permit_five,
        )
        outcome(
            "oracle-outage",
            unresolved == "UNKNOWN"
            and await adapter.query_effect(effect_five) == 5,
            "no success claim while the independent oracle is unreachable",
        )
        recovered_oracle = await reconcile_effect(sessions, adapter, effect_five)
        outcome(
            "oracle-recovered",
            recovered_oracle == "VERIFIED",
            "original committed effect verified after read-only oracle recovery",
        )

        # Simulate a provider which commits but has forgotten the effect ID
        # before the client gets an acknowledgement. Repeated reconciliation
        # may inspect state, but MUST NOT blindly call apply again.
        permit_six = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "f" * 64,
        )

        class ForgottenStatusAfterCommit(SimulatorAdapter):
            async def apply(self, effect_id, permit):
                await super().apply(effect_id, permit)
                raise TimeoutError("effect committed, acknowledgement lost")

            async def query_effect(self, effect_id):
                return None

        missing_status = ForgottenStatusAfterCommit(sessions)
        effect_six = await prepare_receipt(sessions, permit_six)
        first_unknown = await execute_simulator_effect(
            sessions, missing_status, permit_six,
        )
        second_unknown = await reconcile_effect(
            sessions, missing_status, effect_six,
        )
        async with sessions() as session:
            receipt_six = await session.get(EffectReceipt, effect_six)
            single_send = receipt_six.attempt_count == 1
        outcome(
            "retention-gap",
            first_unknown == second_unknown == "UNKNOWN"
            and single_send
            and await adapter.observe(tenant, target_id) == (6, True),
            "missing remote idempotency retention is visible as UNKNOWN",
        )
        recovered_six = await reconcile_effect(sessions, adapter, effect_six)
        outcome(
            "retention-recovered",
            recovered_six == "VERIFIED"
            and await adapter.query_effect(effect_six) == 6,
            "original effect ID resolves when status storage returns",
        )

        # A target edit outside the worker's action path must invalidate the
        # previously minted target-version permit even with a fresh fence.
        permit_seven = await reserve_simulator_target(
            sessions, tenant, target_id, incident_id, "7" * 64,
        )
        async with sessions() as session:
            async with session.begin():
                external_edit = await session.get(
                    SimulatorTarget, (tenant, target_id), with_for_update=True,
                )
                external_edit.version += 1
        try:
            await adapter.apply(uuid4(), permit_seven)
            outcome(
                "external-version-edit", False,
                "an external target-version edit was incorrectly accepted",
            )
        except TargetVersionConflict:
            outcome(
                "external-version-edit", True,
                "adapter CAS rejects a target modified outside the workflow",
            )
    finally:
        # Remove disposable fixture rows. Keep only the digest + scenario report.
        async with sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(SimulatorAppliedEffect).where(
                        SimulatorAppliedEffect.tenant_id == tenant
                    )
                )
                await session.execute(
                    delete(EffectReceipt).where(EffectReceipt.tenant_id == tenant)
                )
                await session.execute(
                    delete(ResourceReservation).where(
                        ResourceReservation.tenant_id == tenant
                    )
                )
                await session.execute(
                    delete(ResourceEpoch).where(
                        ResourceEpoch.tenant_id == tenant
                    )
                )
                await session.execute(
                    delete(SimulatorTarget).where(
                        SimulatorTarget.tenant_id == tenant
                    )
                )
                if incident_id:
                    await session.execute(
                        delete(PolicyAssignment).where(
                            PolicyAssignment.tenant_id == tenant,
                            PolicyAssignment.incident_id == incident_id,
                        )
                    )
                    await session.execute(
                        delete(UIEvent).where(UIEvent.incident_id == incident_id)
                    )
                    await session.execute(
                        delete(OutboxEntry).where(
                            OutboxEntry.incident_id == incident_id
                        )
                    )
                await session.execute(
                    delete(AdmissionUsage).where(
                        AdmissionUsage.tenant_id == tenant
                    )
                )
                await session.execute(
                    delete(Incident).where(Incident.tenant_id == tenant)
                )

    run = AdapterConformanceRun(
        id=uuid4(), adapter_id="simulator-v1",
        code_digest=simulator_digest(), suite_version=SUITE_VERSION,
        retention_seconds=RETENTION_SECONDS, results=results,
        state="LAB_PASS" if complete_report(results) else "LAB_FAIL",
    )
    async with sessions() as session:
        async with session.begin():
            session.add(run)
    return run


async def main() -> None:
    from backend.database import get_session_maker

    run = await execute_suite(get_session_maker())
    print(json.dumps({
        "run_id": str(run.id), "adapter": run.adapter_id,
        "code_digest": run.code_digest, "state": run.state,
        "results": run.results,
    }, indent=2))
    if run.state != "LAB_PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())


async def current_simulator_is_conformant(
    session: AsyncSession,
) -> bool:
    """Check current local code digest and recent passing test evidence."""
    from backend.adapters.simulator import utc

    run = await session.scalar(
        select(AdapterConformanceRun)
        .where(AdapterConformanceRun.adapter_id == "simulator-v1")
        .order_by(AdapterConformanceRun.created_at.desc(), AdapterConformanceRun.id)
        .limit(1)
    )
    if run is None:
        return False
    age = datetime.now(timezone.utc) - utc(run.created_at)
    return (
        run.state == "LAB_PASS"
        and run.code_digest == simulator_digest()
        and run.suite_version == SUITE_VERSION
        and run.retention_seconds >= RETENTION_SECONDS
        and timedelta(0) <= age <= timedelta(days=7)
        and complete_report(run.results)
    )
