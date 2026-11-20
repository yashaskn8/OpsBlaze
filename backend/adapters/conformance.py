
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
    Permit, SimulatorAdapter, StaleFence, TargetVersionConflict,
)
from backend.effects.reservations import reserve_simulator_target
from backend.models import (
    AdapterConformanceRun, AdmissionUsage, Incident, OutboxEntry,
    ResourceEpoch, ResourceReservation, SimulatorAppliedEffect, SimulatorTarget,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings

SUITE_VERSION = "simulator-conformance-v1"
RETENTION_SECONDS = 3600


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
        state="LAB_PASS" if results and all(r["passed"] for r in results) else "LAB_FAIL",
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
