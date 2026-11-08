"""Unit tests for A1: Budgeted Adaptive Probe Planner.

Verifies:
- Iterative hypothesis ranking and discriminating probe selection.
- Multi-step investigation converges and proposes a verified plan.
- InvestigationStep persistence with loop_state, step_no, and prompt_digest.
- Idempotent resumption from max(step_no) on worker recovery.
- Calibrated abstention integration when evidence is inconclusive or budget exhausted.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import DisposableTargetSimulator
from backend.budget.manager import DurableBudgetManager
from backend.domain.investigation import (
    HypothesisItem,
    InvestigationLoopState,
    InvestigationModelResponse,
    InvestigationResult,
    ModelAdapterProtocol,
    ProbeSelection,
    ProposedPlan,
)
from backend.evidence.ledger import ContextManifestBuilder, EvidenceLedgerService
from backend.investigator.abstention import CalibratedAbstentionGate
from backend.investigator.planner import AdaptiveProbePlanner
from backend.persistence.models import CapabilityGrant, EvidenceLedger, Incident, InvestigationStep
from backend.tools.catalog import ToolCatalog
from backend.tools.gateway import CapabilityGateway


class MockDeterministicModelAdapter(ModelAdapterProtocol):
    """Scripted model adapter for deterministic multi-step testing."""

    def __init__(self) -> None:
        self.call_count = 0

    async def analyze_context(
        self,
        incident_id: uuid.UUID,
        rendered_context: str,
        tool_catalog_summary: list[dict[str, Any]],
    ) -> InvestigationModelResponse:
        self.call_count += 1
        if self.call_count == 1:
            # Step 1: Low confidence, selects discriminating probe
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(
                        id="H1",
                        title="OOM Killed Pod",
                        probability=0.50,
                        rationale="High memory alerts observed",
                    ),
                    HypothesisItem(
                        id="H2",
                        title="Network Connection Reset",
                        probability=0.50,
                        rationale="Upstream timeout errors",
                    ),
                ],
                selected_probe=ProbeSelection(
                    tool_id="telemetry_sample",
                    target_id="k8s:pod:order-service-9f",
                    parameters={"metric": "container_memory_working_set_bytes"},
                    rationale="Check actual memory usage to confirm OOM",
                ),
                proposed_plan=None,
                rationale="Need telemetry probe to discriminate H1 vs H2",
                tokens_used=800,
            )
        else:
            # Step 2: High confidence from telemetry evidence -> Propose plan citing verified evidence ID
            ev_match = re.search(r'<untrusted_observation id="([^"]+)"', rendered_context)
            ev_ids = [uuid.UUID(ev_match.group(1))] if ev_match else []

            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(
                        id="H1",
                        title="OOM Killed Pod",
                        probability=0.90,
                        supporting_evidence_ids=ev_ids,
                        rationale="Telemetry confirms memory breached limit at 99.8%",
                    ),
                    HypothesisItem(
                        id="H2",
                        title="Network Connection Reset",
                        probability=0.10,
                        supporting_evidence_ids=[],
                        rationale="Disproven by memory telemetry",
                    ),
                ],
                selected_probe=None,
                proposed_plan=ProposedPlan(
                    target_id="k8s:pod:order-service-9f",
                    plan_digest="sha256-plan-oom-restart",
                    action_digest="sha256-act-restart",
                    expected_version="v2.4.1",
                    action_kind="RESTART_POD",
                    parameters={"grace_period_seconds": 30},
                    supporting_evidence_ids=ev_ids,
                ),
                rationale="High confidence OOM root cause confirmed by evidence",
                tokens_used=950,
            )


@pytest.mark.asyncio
async def test_adaptive_probe_planner_converges_and_persists_steps(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A1: Planner executes multi-step investigation loop and successfully proposes plan."""
    incident_id = uuid.uuid4()
    target_id = "k8s:pod:order-service-9f"

    # Setup database records
    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_a1_1",
            target_id=target_id,
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(inc)

        catalog = ToolCatalog.create_default()
        await catalog.sync_to_db(session)

        # Grant telemetry_sample capability
        session.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()

    gateway = CapabilityGateway(session_factory, catalog)
    budget_mgr = DurableBudgetManager(session_factory)
    evidence_svc = EvidenceLedgerService(session_factory)  # note: or session
    manifest_builder = ContextManifestBuilder()
    model_adapter = MockDeterministicModelAdapter()
    abstention_gate = CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25)

    simulator = DisposableTargetSimulator()
    planner = AdaptiveProbePlanner(
        session_factory=session_factory,
        budget_manager=budget_mgr,
        evidence_service=evidence_svc,
        capability_gateway=gateway,
        context_builder=manifest_builder,
        model_adapter=model_adapter,
        abstention_gate=abstention_gate,
        target_adapter=simulator,
    )

    result = await planner.run_investigation_loop(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        target_id=target_id,
        incident_title="Memory Leak in Order Service",
        max_steps=5,
    )

    assert isinstance(result, InvestigationResult)
    assert result.is_proposed is True
    assert result.is_abstained is False
    assert result.status == InvestigationLoopState.PROPOSING
    assert result.proposed_plan is not None
    assert result.proposed_plan.action_kind == "RESTART_POD"
    assert result.steps_taken == 2

    # Verify investigation_step records in PostgreSQL
    async with session_factory() as session:
        steps_res = await session.execute(
            select(InvestigationStep)
            .where(
                InvestigationStep.tenant_id == isolated_tenant,
                InvestigationStep.incident_id == incident_id,
            )
            .order_by(InvestigationStep.step_no)
        )
        steps = list(steps_res.scalars().all())
        assert len(steps) == 2
        assert steps[0].step_no == 1
        assert steps[0].loop_state == InvestigationLoopState.PROBING.value
        assert steps[0].selected_probe is not None
        assert steps[1].step_no == 2
        assert steps[1].loop_state == InvestigationLoopState.PROPOSING.value
        assert steps[1].proposed_plan_json is not None


@pytest.mark.asyncio
async def test_adaptive_probe_planner_resumes_idempotently(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A1: When re-executed after a crash, planner starts from max(step_no) + 1."""
    incident_id = uuid.uuid4()
    target_id = "k8s:pod:order-service-9f"

    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_a1_resume",
            target_id=target_id,
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(inc)

        catalog = ToolCatalog.create_default()
        await catalog.sync_to_db(session)

        session.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )

        # Pre-seed step 1 and its evidence observation as if executed before crash
        ev_id = uuid.uuid4()
        session.add(
            EvidenceLedger(
                id=ev_id,
                tenant_id=isolated_tenant,
                incident_id=incident_id,
                source="telemetry_sample",
                source_version="1.0.0",
                target_id=target_id,
                target_version="1.0",
                observed_at=datetime.now(UTC),
                captured_at=datetime.now(UTC),
                sha256_digest="abc12345",
                raw_payload_redacted={"metric": "cpu", "value": 99.0},
                classification="TELEMETRY",
                validity_status="VALID",
            )
        )
        step1 = InvestigationStep(
            id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=incident_id,
            step_no=1,
            loop_state=InvestigationLoopState.PROBING.value,
            prompt_digest="sha256-prior-step1",
            evidence_ids_used=[str(ev_id)],
            token_count=1000,
            hypotheses_json={"hypotheses": []},
            selected_probe={
                "tool_id": "telemetry_sample",
                "target_id": target_id,
                "parameters": {},
            },
            proposed_plan_json=None,
            abstention_reason=None,
        )
        session.add(step1)
        await session.commit()

    gateway = CapabilityGateway(session_factory, catalog)
    budget_mgr = DurableBudgetManager(session_factory)
    evidence_svc = EvidenceLedgerService(session_factory)
    manifest_builder = ContextManifestBuilder()

    # Model adapter that immediately proposes on step 2 citing verified evidence
    class ResumeModelAdapter(ModelAdapterProtocol):
        async def analyze_context(
            self,
            incident_id: uuid.UUID,
            rendered_context: str,
            tool_catalog_summary: list[dict[str, Any]],
        ) -> InvestigationModelResponse:
            ev_match = re.search(r'<untrusted_observation id="([^"]+)"', rendered_context)
            ev_ids = [uuid.UUID(ev_match.group(1))] if ev_match else []
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(
                        id="H1",
                        title="OOM Killed Pod",
                        probability=0.95,
                        supporting_evidence_ids=ev_ids,
                        rationale="Pre-existing evidence confirms OOM",
                    )
                ],
                selected_probe=None,
                proposed_plan=ProposedPlan(
                    target_id=target_id,
                    plan_digest="sha256-plan-resumed",
                    action_digest="sha256-act-resumed",
                    expected_version="v2.4.1",
                    action_kind="RESTART_POD",
                    supporting_evidence_ids=ev_ids,
                ),
                tokens_used=750,
            )

    planner = AdaptiveProbePlanner(
        session_factory=session_factory,
        budget_manager=budget_mgr,
        evidence_service=evidence_svc,
        capability_gateway=gateway,
        context_builder=manifest_builder,
        model_adapter=ResumeModelAdapter(),
        abstention_gate=CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25),
        target_adapter=DisposableTargetSimulator(),
    )

    result = await planner.run_investigation_loop(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        target_id=target_id,
        incident_title="Resumed Investigation",
        max_steps=5,
    )

    assert result.is_proposed is True
    assert result.steps_taken == 2  # executed step 2 (resuming after step 1)

    async with session_factory() as session:
        steps_res = await session.execute(
            select(InvestigationStep)
            .where(
                InvestigationStep.tenant_id == isolated_tenant,
                InvestigationStep.incident_id == incident_id,
            )
            .order_by(InvestigationStep.step_no)
        )
        steps = list(steps_res.scalars().all())
        assert len(steps) == 2
        assert steps[0].step_no == 1
        assert steps[1].step_no == 2


@pytest.mark.asyncio
async def test_adaptive_probe_planner_detects_probe_loop_and_abstains(
    session_factory: async_sessionmaker[AsyncSession], isolated_tenant: uuid.UUID
) -> None:
    """A1/A4: Planner detects repeating probe without new evidence and abstains with UNRESOLVABLE_CONFLICT."""
    incident_id = uuid.uuid4()
    target_id = "k8s:pod:order-service-9f"

    async with session_factory() as session:
        inc = Incident(
            id=incident_id,
            tenant_id=isolated_tenant,
            client_key=f"key-{incident_id.hex[:6]}",
            request_sha256="reqhash_a1_loop",
            target_id=target_id,
            status_projection="INVESTIGATING",
            status_version=1,
        )
        session.add(inc)

        catalog = ToolCatalog.create_default()
        await catalog.sync_to_db(session)

        session.add(
            CapabilityGrant(
                id=uuid.uuid4(),
                tenant_id=isolated_tenant,
                incident_id=incident_id,
                tool_id="telemetry_sample",
                tool_version="v1",
                target_id=target_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()

    gateway = CapabilityGateway(session_factory, catalog)
    budget_mgr = DurableBudgetManager(session_factory)
    evidence_svc = EvidenceLedgerService(session_factory)
    manifest_builder = ContextManifestBuilder()

    # Loop adapter: proposes the exact same probe repeatedly
    class LoopingModelAdapter(ModelAdapterProtocol):
        async def analyze_context(
            self,
            incident_id: uuid.UUID,
            rendered_context: str,
            tool_catalog_summary: list[dict[str, Any]],
        ) -> InvestigationModelResponse:
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(id="H1", title="Stuck Loop", probability=0.50),
                    HypothesisItem(id="H2", title="Unknown Error", probability=0.50),
                ],
                selected_probe=ProbeSelection(
                    tool_id="telemetry_sample",
                    target_id=target_id,
                    parameters={"metric": "cpu"},
                ),
                proposed_plan=None,
                tokens_used=500,
            )

    planner = AdaptiveProbePlanner(
        session_factory=session_factory,
        budget_manager=budget_mgr,
        evidence_service=evidence_svc,
        capability_gateway=gateway,
        context_builder=manifest_builder,
        model_adapter=LoopingModelAdapter(),
        abstention_gate=CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25),
        target_adapter=DisposableTargetSimulator(),
    )

    result = await planner.run_investigation_loop(
        tenant_id=isolated_tenant,
        incident_id=incident_id,
        target_id=target_id,
        incident_title="Looping Incident",
        max_steps=5,
    )

    assert result.is_proposed is False
    assert result.is_abstained is True
    assert result.abstention_reason == "UNRESOLVABLE_CONFLICT"
