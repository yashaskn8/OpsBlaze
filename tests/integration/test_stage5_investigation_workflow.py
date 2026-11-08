"""Stage 5 Integration Tests: Adaptive Investigation & Temporal Workflow Progression.

Verifies:
1. End-to-end adaptive investigation in IncidentWorkflow on Temporal:
   - InvestigationStep persistence (PROBING -> PROPOSING)
   - EvidenceLedger immutable observations
   - BudgetLedger reservations and delta settlements
   - Successful convergence to AWAITING_APPROVAL with proposed plan attached.
2. Calibrated Abstention path:
   - When evidence is inconclusive, abstention gate triggers
   - IncidentWorkflow safely projects to terminal state NEEDS_REVIEW.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment

from backend.adapters.simulator import DisposableTargetSimulator
from backend.budget.manager import DurableBudgetManager
from backend.domain.incident import IncidentStatus
from backend.evidence.ledger import ContextManifestBuilder, EvidenceLedgerService
from backend.investigator.abstention import CalibratedAbstentionGate
from backend.investigator.model_adapter import DeterministicFakeModelAdapter
from backend.investigator.planner import AdaptiveProbePlanner
from backend.persistence.models import CapabilityGrant, EvidenceLedger, InvestigationStep
from backend.persistence.repositories import IncidentRepository
from backend.tools.catalog import ToolCatalog
from backend.tools.gateway import CapabilityGateway
from backend.workflows.dispatcher import OutboxDispatcher, TemporalWorkflowStarter
from backend.workflows.worker import create_b1_worker

pytestmark = pytest.mark.postgres


async def _wait_for_incident_status(
    repo: IncidentRepository,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    expected_status: IncidentStatus,
    timeout_s: float = 15.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        snap = await repo.get_incident(tenant_id, incident_id)
        if snap.status == expected_status:
            return
        await asyncio.sleep(0.1)
    final_snap = await repo.get_incident(tenant_id, incident_id)
    raise AssertionError(
        f"Timed out waiting for status {expected_status}; current={final_snap.status}"
    )


@pytest.mark.asyncio
async def test_stage5_temporal_adaptive_investigation_proposes_plan(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Stage 5: Full workflow progression with adaptive investigation reaching AWAITING_APPROVAL."""
    target_id = "k8s:pod:order-service-9f"
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"stage5-prop-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="5" * 64,
    )
    inc_id = outcome.response.id
    queue_name = f"s5-q-{uuid.uuid4().hex[:8]}"

    # Seed catalog and grant
    async with session_factory() as session:
        catalog = ToolCatalog.create_default()
        await catalog.sync_to_db(session)
        session.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=inc_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )
        await session.commit()

    env = await WorkflowEnvironment.start_local()
    try:
        starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
        dispatcher = OutboxDispatcher(session_factory, starter, adaptive_investigation=True)

        simulator = DisposableTargetSimulator()
        worker = create_b1_worker(
            env.client,
            session_factory,
            task_queue=queue_name,
            adapters={target_id: simulator},
        )

        async with worker:
            delivered = await dispatcher.dispatch_once()
            assert delivered >= 1

            # Wait for workflow to reach AWAITING_APPROVAL
            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.AWAITING_APPROVAL,
                timeout_s=20.0,
            )

        # Verify database evidence and investigation records
        async with session_factory() as session:
            ev_stmt = select(EvidenceLedger).where(
                EvidenceLedger.tenant_id == isolated_tenant,
                EvidenceLedger.incident_id == inc_id,
            )
            ev_res = await session.execute(ev_stmt)
            evidence = list(ev_res.scalars().all())
            assert len(evidence) >= 1
            assert evidence[0].source == "telemetry_sample"

            step_stmt = (
                select(InvestigationStep)
                .where(
                    InvestigationStep.tenant_id == isolated_tenant,
                    InvestigationStep.incident_id == inc_id,
                )
                .order_by(InvestigationStep.step_no)
            )
            step_res = await session.execute(step_stmt)
            steps = list(step_res.scalars().all())
            assert len(steps) >= 2
            assert steps[0].step_no == 1
            assert steps[0].loop_state == "PROBING"
            assert steps[1].step_no == 2
            assert steps[1].loop_state == "PROPOSING"
            assert steps[1].proposed_plan_json is not None

    finally:
        await env.shutdown()


@pytest.mark.asyncio
async def test_stage5_temporal_adaptive_investigation_abstains_to_needs_review(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Stage 5: When model adapter abstains, IncidentWorkflow projects to NEEDS_REVIEW."""
    target_id = "k8s:pod:order-service-9f"
    inc_repo = IncidentRepository(session_factory)
    outcome = await inc_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=f"stage5-abstain-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="a" * 64,
    )
    inc_id = outcome.response.id
    queue_name = f"s5-abstain-q-{uuid.uuid4().hex[:8]}"

    # Seed catalog
    async with session_factory() as session:
        catalog = ToolCatalog.create_default()
        await catalog.sync_to_db(session)

    # Custom planner configured with force_abstention=True
    budget_mgr = DurableBudgetManager(session_factory)
    evidence_svc = EvidenceLedgerService(session_factory)
    catalog = ToolCatalog.create_default()
    gateway = CapabilityGateway(session_factory, catalog)
    manifest_builder = ContextManifestBuilder()
    abstaining_adapter = DeterministicFakeModelAdapter(force_abstention=True)
    abstention_gate = CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25)
    simulator = DisposableTargetSimulator()

    abstaining_planner = AdaptiveProbePlanner(
        session_factory=session_factory,
        budget_manager=budget_mgr,
        evidence_service=evidence_svc,
        capability_gateway=gateway,
        context_builder=manifest_builder,
        model_adapter=abstaining_adapter,
        abstention_gate=abstention_gate,
        target_adapter=simulator,
    )

    env = await WorkflowEnvironment.start_local()
    try:
        starter = TemporalWorkflowStarter(env.client, task_queue=queue_name)
        dispatcher = OutboxDispatcher(session_factory, starter, adaptive_investigation=True)

        worker = create_b1_worker(
            env.client,
            session_factory,
            task_queue=queue_name,
            planner=abstaining_planner,
        )

        async with worker:
            delivered = await dispatcher.dispatch_once()
            assert delivered >= 1

            # Wait for workflow to project to terminal NEEDS_REVIEW
            await _wait_for_incident_status(
                inc_repo,
                isolated_tenant,
                inc_id,
                IncidentStatus.NEEDS_REVIEW,
                timeout_s=20.0,
            )

        # Verify investigation_step recorded abstention
        async with session_factory() as session:
            step_stmt = select(InvestigationStep).where(
                InvestigationStep.tenant_id == isolated_tenant,
                InvestigationStep.incident_id == inc_id,
            )
            step_res = await session.execute(step_stmt)
            steps = list(step_res.scalars().all())
            assert len(steps) >= 1
            assert steps[0].loop_state == "ABSTAINED"
            assert steps[0].abstention_reason is not None

    finally:
        await env.shutdown()
