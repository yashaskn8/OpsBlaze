"""Typed Temporal activities for B1 Durable Incident Workflow and Stage 3 (B2 + B3 + E8)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from backend.adapters.protocol import TargetAdapterProtocol
from backend.adapters.simulator import DisposableTargetSimulator
from backend.budget.manager import DurableBudgetManager
from backend.domain.incident import (
    ApprovedExecutionSignalInput,
    DomainError,
    IncidentStatus,
    Stage3DispatchActivityOutput,
    Stage3VerifyActivityOutput,
    StatusProjectionActivityInput,
    StatusProjectionActivityOutput,
    WorkflowIncidentInput,
    make_incident_workflow_id,
)
from backend.effects.fencing import (
    ApprovedPlanContext,
    ResourceReservationManager,
)
from backend.effects.receipts import (
    ActionGateway,
    ActionSpec,
    EffectReconciler,
    EffectState,
)
from backend.evidence.ledger import ContextManifestBuilder, EvidenceLedgerService
from backend.investigator.abstention import CalibratedAbstentionGate
from backend.investigator.fake import FakeInvestigator
from backend.investigator.model_adapter import DeterministicFakeModelAdapter
from backend.investigator.planner import AdaptiveProbePlanner
from backend.observability.metrics import emit_event, metrics
from backend.persistence.repositories import IncidentRepository
from backend.tools.catalog import ToolCatalog
from backend.tools.gateway import CapabilityGateway

B1_TASK_QUEUE = "opsblaze-b1-incident-queue"


@dataclass(frozen=True)
class FakeInvestigationActivityOutput:
    incident_id: str
    tenant_id: str
    target_id: str
    hypothesis: str
    recommended_status: str
    observation_count: int
    observation_digests: list[str]


@dataclass(frozen=True)
class AdaptiveInvestigationActivityInput:
    tenant_id: str
    incident_id: str
    target_id: str
    incident_title: str
    runbook_revision: str | None = None
    max_steps: int = 5
    correlation_id: str | None = None


@dataclass(frozen=True)
class AdaptiveInvestigationActivityOutput:
    incident_id: str
    tenant_id: str
    target_id: str
    status: str
    is_proposed: bool
    is_abstained: bool
    hypothesis: str
    abstention_reason: str | None
    observation_digests: list[str]
    steps_taken: int
    proposed_plan: dict[str, Any] | None = None


class B1Activities:
    """Dependency-injected Temporal activity collection for B1 and Stage 3."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        investigator: FakeInvestigator | None = None,
        adapters: dict[str, TargetAdapterProtocol] | None = None,
        planner: AdaptiveProbePlanner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._incident_repo = IncidentRepository(session_factory)
        self._investigator = investigator or FakeInvestigator()
        self._adapters: dict[str, TargetAdapterProtocol] = dict(adapters or {})
        self._reservation_mgr = ResourceReservationManager(session_factory)
        self._gateway = ActionGateway(session_factory)
        self._reconciler = EffectReconciler(session_factory)
        self._planner = planner

    @activity.defn(name="b1_project_incident_status")
    async def project_incident_status(
        self, act_input: StatusProjectionActivityInput
    ) -> StatusProjectionActivityOutput:
        info = activity.info()
        wf_id = make_incident_workflow_id(
            uuid.UUID(act_input.tenant_id), uuid.UUID(act_input.incident_id)
        )
        if info.attempt > 1:
            metrics.inc("activity_retries_total")
            emit_event(
                "activity.retry",
                incident_id=act_input.incident_id,
                workflow_id=wf_id,
                correlation_id=act_input.correlation_id,
                activity_type="b1_project_incident_status",
                attempt=info.attempt,
                target_status=act_input.target_status,
                level="WARNING",
            )

        target_status = IncidentStatus(act_input.target_status)
        resp = await self._incident_repo.transition_status(
            tenant_id=uuid.UUID(act_input.tenant_id),
            incident_id=uuid.UUID(act_input.incident_id),
            target_status=target_status,
            expected_version=act_input.expected_version,
            allow_cancelled_noop=True,
            correlation_id=act_input.correlation_id,
        )
        is_cancelled = resp.status == IncidentStatus.CANCELLED
        return StatusProjectionActivityOutput(
            incident_id=str(resp.id),
            status=resp.status.value,
            status_version=resp.status_version,
            cancelled=is_cancelled,
        )

    @activity.defn(name="b1_run_fake_investigation")
    async def run_fake_investigation(
        self, wf_input: WorkflowIncidentInput
    ) -> FakeInvestigationActivityOutput:
        info = activity.info()
        wf_id = make_incident_workflow_id(
            uuid.UUID(wf_input.tenant_id), uuid.UUID(wf_input.incident_id)
        )
        if info.attempt > 1:
            metrics.inc("activity_retries_total")
            emit_event(
                "activity.retry",
                incident_id=wf_input.incident_id,
                workflow_id=wf_id,
                correlation_id=wf_input.correlation_id,
                activity_type="b1_run_fake_investigation",
                attempt=info.attempt,
                level="WARNING",
            )

        result = self._investigator.investigate(wf_input)
        return FakeInvestigationActivityOutput(
            incident_id=result.incident_id,
            tenant_id=result.tenant_id,
            target_id=result.target_id,
            hypothesis=result.hypothesis,
            recommended_status=result.recommended_status.value,
            observation_count=len(result.observations),
            observation_digests=[o.deterministic_digest for o in result.observations],
        )

    @activity.defn(name="stage5_run_adaptive_investigation")
    async def run_adaptive_investigation(
        self, act_input: AdaptiveInvestigationActivityInput
    ) -> AdaptiveInvestigationActivityOutput:
        """Stage 5 activity: Iterative, bounded, hypothesis-discriminating adaptive probe planner."""
        info = activity.info()
        wf_id = make_incident_workflow_id(
            uuid.UUID(act_input.tenant_id), uuid.UUID(act_input.incident_id)
        )
        if info.attempt > 1:
            metrics.inc("activity_retries_total")
            emit_event(
                "activity.retry",
                incident_id=act_input.incident_id,
                workflow_id=wf_id,
                correlation_id=act_input.correlation_id,
                activity_type="stage5_run_adaptive_investigation",
                attempt=info.attempt,
                level="WARNING",
            )

        activity.heartbeat(f"Starting adaptive investigation for {act_input.incident_id}")

        planner = self._planner
        if planner is None:
            budget_mgr = DurableBudgetManager(self._session_factory)
            evidence_svc = EvidenceLedgerService(self._session_factory)
            catalog = ToolCatalog.create_default()
            gateway = CapabilityGateway(self._session_factory, catalog)
            manifest_builder = ContextManifestBuilder()
            model_adapter = DeterministicFakeModelAdapter()
            abstention_gate = CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25)
            target_adapter = self._adapters.get(act_input.target_id) or DisposableTargetSimulator()
            planner = AdaptiveProbePlanner(
                session_factory=self._session_factory,
                budget_manager=budget_mgr,
                evidence_service=evidence_svc,
                capability_gateway=gateway,
                context_builder=manifest_builder,
                model_adapter=model_adapter,
                abstention_gate=abstention_gate,
                target_adapter=target_adapter,
            )

        res = await planner.run_investigation_loop(
            tenant_id=uuid.UUID(act_input.tenant_id),
            incident_id=uuid.UUID(act_input.incident_id),
            target_id=act_input.target_id,
            incident_title=act_input.incident_title,
            runbook_revision=act_input.runbook_revision,
            max_steps=act_input.max_steps,
        )

        top_h = res.hypotheses[0].title if res.hypotheses else "No hypothesis formed"
        plan_dict = (
            res.proposed_plan.model_dump(mode="json") if res.proposed_plan else None
        )

        evidence_svc = EvidenceLedgerService(self._session_factory)
        valid_ev = await evidence_svc.query_valid_evidence(
            tenant_id=uuid.UUID(act_input.tenant_id),
            incident_id=uuid.UUID(act_input.incident_id),
        )
        obs_digests = [e.sha256_digest for e in valid_ev]

        return AdaptiveInvestigationActivityOutput(
            incident_id=act_input.incident_id,
            tenant_id=act_input.tenant_id,
            target_id=act_input.target_id,
            status=res.status.value,
            is_proposed=res.is_proposed,
            is_abstained=res.is_abstained,
            hypothesis=top_h,
            abstention_reason=res.abstention_reason,
            observation_digests=obs_digests,
            steps_taken=res.steps_taken,
            proposed_plan=plan_dict,
        )

    @activity.defn(name="stage3_execute_approved_action")
    async def execute_approved_action(
        self, act_input: ApprovedExecutionSignalInput
    ) -> Stage3DispatchActivityOutput:
        """Stage 3 activity: P2 approval check -> E8 conformance gate -> B2 reservation ->
        B3 receipt intent -> ActionGateway dispatch (with UNKNOWN reconciliation if needed).
        """
        tenant_id = uuid.UUID(act_input.tenant_id)
        incident_id = uuid.UUID(act_input.incident_id)
        adapter = self._adapters.get(act_input.adapter_id)
        if adapter is None:
            return Stage3DispatchActivityOutput(
                receipt_id="",
                effect_state=EffectState.FAILED.value,
                reservation_id="",
                fence=0,
                adapter_id=act_input.adapter_id,
                error_detail=f"Adapter '{act_input.adapter_id}' is not registered",
            )

        expires_at = (
            datetime.fromisoformat(act_input.expires_at_iso)
            if act_input.expires_at_iso
            else datetime.now(UTC)
        )
        approval = ApprovedPlanContext(
            approval_id=uuid.UUID(act_input.approval_id),
            tenant_id=tenant_id,
            incident_id=incident_id,
            target_id=act_input.target_id,
            plan_digest=act_input.plan_digest,
            action_digest=act_input.action_digest,
            expected_version=act_input.expected_version,
            approved_by_role=act_input.approved_by_role,
            execution_mode=act_input.execution_mode,
            approved_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        action = ActionSpec(
            action_kind=act_input.action_kind,
            parameters=dict(act_input.parameters),
        )

        try:
            permit = await self._reservation_mgr.acquire_reservation(
                approval=approval,
                adapter=adapter,
                correlation_id=act_input.correlation_id,
            )
            receipt = await self._gateway.execute_with_permit(
                permit=permit,
                approval=approval,
                action=action,
                adapter=adapter,
                verify_immediately=False,
                correlation_id=act_input.correlation_id,
            )
            # If the receipt landed in UNKNOWN (e.g., lost response after commit), reconcile it
            if receipt.state == EffectState.UNKNOWN:
                for _ in range(3):
                    reconciled_list = await self._reconciler.reconcile_pending(
                        adapters=self._adapters,
                        tenant_id=tenant_id,
                        incident_id=incident_id,
                        correlation_id=act_input.correlation_id,
                    )
                    if reconciled_list:
                        receipt = reconciled_list[0]
                    if receipt.state != EffectState.UNKNOWN:
                        break

            return Stage3DispatchActivityOutput(
                receipt_id=str(receipt.id),
                effect_state=receipt.state.value,
                reservation_id=str(permit.reservation_id),
                fence=permit.fence,
                adapter_id=act_input.adapter_id,
                observed_version=receipt.observed_version or "",
                error_detail=receipt.last_error or "",
            )
        except DomainError as exc:
            return Stage3DispatchActivityOutput(
                receipt_id="",
                effect_state=EffectState.FAILED.value,
                reservation_id="",
                fence=0,
                adapter_id=act_input.adapter_id,
                error_detail=str(exc),
            )

    @activity.defn(name="stage3_verify_and_finalize_effect")
    async def verify_and_finalize_effect(
        self, act_input: Stage3DispatchActivityOutput
    ) -> Stage3VerifyActivityOutput:
        """Stage 3 activity: Run IndependentPostconditionVerifier on CONFIRMED receipt and
        finalize to VERIFIED or NEEDS_REVIEW while releasing the B2 reservation.
        """
        adapter = self._adapters.get(act_input.adapter_id)
        if adapter is None or not act_input.receipt_id:
            return Stage3VerifyActivityOutput(
                receipt_id=act_input.receipt_id,
                effect_state=EffectState.NEEDS_REVIEW.value,
                final_incident_status=IncidentStatus.NEEDS_REVIEW.value,
                detail="Missing adapter or receipt_id for postcondition verification",
            )

        receipt_uuid = uuid.UUID(act_input.receipt_id)
        # Look up receipt across session to get tenant_id
        from sqlalchemy import select

        from backend.persistence.models import EffectReceipt

        async with self._session_factory() as session:
            row = (
                await session.execute(select(EffectReceipt).where(EffectReceipt.id == receipt_uuid))
            ).scalar_one_or_none()
            if row is None:
                return Stage3VerifyActivityOutput(
                    receipt_id=act_input.receipt_id,
                    effect_state=EffectState.NEEDS_REVIEW.value,
                    final_incident_status=IncidentStatus.NEEDS_REVIEW.value,
                    detail="Receipt not found in PostgreSQL",
                )
            tenant_id = row.tenant_id

        final_receipt = await self._gateway.verify_and_finalize_confirmed_receipt(
            receipt_id=receipt_uuid,
            tenant_id=tenant_id,
            permit=None,
            adapter=adapter,
        )
        final_inc_status = (
            IncidentStatus.VERIFIED.value
            if final_receipt.state == EffectState.VERIFIED
            else IncidentStatus.NEEDS_REVIEW.value
        )
        return Stage3VerifyActivityOutput(
            receipt_id=str(final_receipt.id),
            effect_state=final_receipt.state.value,
            final_incident_status=final_inc_status,
            observed_version=final_receipt.observed_version or "",
            detail=final_receipt.last_error or "",
        )
