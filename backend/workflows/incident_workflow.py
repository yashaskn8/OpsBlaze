"""Deterministic Temporal workflow definition for OpsBlaze Feature B1 and Stage 3."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.domain.incident import (
        ApprovedExecutionSignalInput,
        IncidentStatus,
        Stage3DispatchActivityOutput,
        Stage3VerifyActivityOutput,
        StatusProjectionActivityInput,
        StatusProjectionActivityOutput,
        WorkflowIncidentInput,
    )
    from backend.workflows.activities import (
        AdaptiveInvestigationActivityInput,
        AdaptiveInvestigationActivityOutput,
        FakeInvestigationActivityOutput,
    )


@dataclass(frozen=True)
class WorkflowStateSnapshot:
    incident_id: str
    tenant_id: str
    status: str
    status_version: int
    cancel_requested: bool
    hypothesis: str
    observation_digests: list[str]


@workflow.defn(name="IncidentWorkflow")
class IncidentWorkflow:
    """Durable B1 + Stage 3 Incident Workflow.

    Strict determinism rules:
    - No SQL, HTTP, LLM, random, or system clock calls inside workflow code.
    - All external state projections, fake investigation probes, and Stage 3 B2/B3/E8
      actions execute via typed activities.
    - Stops at AWAITING_APPROVAL and waits for human resolution, cancellation, or
      an approved simulator execution signal; never performs unapproved external mutations.
    """

    def __init__(self) -> None:
        self._incident_id: str = ""
        self._tenant_id: str = ""
        self._status: str = IncidentStatus.DISPATCH_PENDING.value
        self._status_version: int = 1
        self._cancel_requested: bool = False
        self._resolution_status: str | None = None
        self._approved_execution: ApprovedExecutionSignalInput | None = None
        self._hypothesis: str = ""
        self._observation_digests: list[str] = []

    @workflow.signal(name="request_cancel")
    def request_cancel(self, reason: str = "") -> None:
        self._cancel_requested = True

    @workflow.signal(name="resolve_awaiting_approval")
    def resolve_awaiting_approval(self, target_terminal_status: str) -> None:
        """Signal used in B1 tests/lifecycle to transition out of AWAITING_APPROVAL
        without executing external B2/B3 mutations (e.g., NEEDS_REVIEW, FAILED, CANCELLED).
        """
        if target_terminal_status == IncidentStatus.CANCELLED.value:
            self._cancel_requested = True
        else:
            self._resolution_status = target_terminal_status

    @workflow.signal(name="approve_and_execute")
    def approve_and_execute(self, execution_input: ApprovedExecutionSignalInput) -> None:
        """Signal used in Stage 3 to submit an approved simulator repair plan at AWAITING_APPROVAL."""
        self._approved_execution = execution_input

    @workflow.query(name="get_workflow_state")
    def get_workflow_state(self) -> WorkflowStateSnapshot:
        return WorkflowStateSnapshot(
            incident_id=self._incident_id,
            tenant_id=self._tenant_id,
            status=self._status,
            status_version=self._status_version,
            cancel_requested=self._cancel_requested,
            hypothesis=self._hypothesis,
            observation_digests=list(self._observation_digests),
        )

    @workflow.run
    async def run(self, wf_input: WorkflowIncidentInput) -> WorkflowStateSnapshot:
        self._incident_id = wf_input.incident_id
        self._tenant_id = wf_input.tenant_id
        retry_policy = RetryPolicy(
            initial_interval=timedelta(milliseconds=100),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=2),
            maximum_attempts=4,
        )

        # Version-aware patch marker for replay compatibility across worker upgrades
        if workflow.patched("b1-workflow-v1"):
            pass

        try:
            if self._cancel_requested:
                return await self._project_cancelled(wf_input, retry_policy)

            # Step 1: Transition to INVESTIGATING
            proj_inv: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=IncidentStatus.INVESTIGATING.value,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_inv.status
            self._status_version = proj_inv.status_version
            if proj_inv.cancelled or self._cancel_requested:
                return await self._project_cancelled(wf_input, retry_policy)

            # Step 2: Investigation progression with replay-safe patch gating
            if wf_input.adaptive_investigation and workflow.patched("stage5-adaptive-investigation-v1"):
                adaptive_out: AdaptiveInvestigationActivityOutput = (
                    await workflow.execute_activity(
                        "stage5_run_adaptive_investigation",
                        AdaptiveInvestigationActivityInput(
                            tenant_id=wf_input.tenant_id,
                            incident_id=wf_input.incident_id,
                            target_id=wf_input.target_id,
                            incident_title=f"Incident {wf_input.incident_id}",
                            max_steps=5,
                            correlation_id=wf_input.correlation_id,
                        ),
                        result_type=AdaptiveInvestigationActivityOutput,
                        start_to_close_timeout=timedelta(seconds=60),
                        heartbeat_timeout=timedelta(seconds=20),
                        retry_policy=retry_policy,
                    )
                )
                self._hypothesis = adaptive_out.hypothesis
                self._observation_digests = list(adaptive_out.observation_digests)

                if self._cancel_requested:
                    return await self._project_cancelled(wf_input, retry_policy)

                if adaptive_out.is_abstained:
                    proj_abstain: StatusProjectionActivityOutput = (
                        await workflow.execute_activity(
                            "b1_project_incident_status",
                            StatusProjectionActivityInput(
                                tenant_id=wf_input.tenant_id,
                                incident_id=wf_input.incident_id,
                                target_status=IncidentStatus.NEEDS_REVIEW.value,
                                expected_version=None,
                                correlation_id=wf_input.correlation_id,
                            ),
                            result_type=StatusProjectionActivityOutput,
                            start_to_close_timeout=timedelta(seconds=10),
                            retry_policy=retry_policy,
                        )
                    )
                    self._status = proj_abstain.status
                    self._status_version = proj_abstain.status_version
                    return WorkflowStateSnapshot(
                        incident_id=wf_input.incident_id,
                        tenant_id=wf_input.tenant_id,
                        status=self._status,
                        status_version=self._status_version,
                        cancel_requested=self._cancel_requested,
                        hypothesis=self._hypothesis,
                        observation_digests=self._observation_digests,
                    )
            else:
                inv_out: FakeInvestigationActivityOutput = (
                    await workflow.execute_activity(
                        "b1_run_fake_investigation",
                        wf_input,
                        result_type=FakeInvestigationActivityOutput,
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=retry_policy,
                    )
                )
                self._hypothesis = inv_out.hypothesis
                self._observation_digests = list(inv_out.observation_digests)

                if self._cancel_requested:
                    return await self._project_cancelled(wf_input, retry_policy)

            # Step 3: Transition to AWAITING_APPROVAL
            proj_await: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=IncidentStatus.AWAITING_APPROVAL.value,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_await.status
            self._status_version = proj_await.status_version
            if proj_await.cancelled or self._cancel_requested:
                return await self._project_cancelled(wf_input, retry_policy)

            # Step 4: Human-wait boundary at AWAITING_APPROVAL.
            await workflow.wait_condition(
                lambda: (
                    self._cancel_requested
                    or self._resolution_status is not None
                    or self._approved_execution is not None
                )
            )

            if self._cancel_requested:
                return await self._project_cancelled(wf_input, retry_policy)

            if self._approved_execution is not None and workflow.patched(
                "stage3-b2-b3-execution-v1"
            ):
                return await self._run_stage3_execution(
                    wf_input, self._approved_execution, retry_policy
                )

            if self._resolution_status is not None:
                proj_res: StatusProjectionActivityOutput = await workflow.execute_activity(
                    "b1_project_incident_status",
                    StatusProjectionActivityInput(
                        tenant_id=wf_input.tenant_id,
                        incident_id=wf_input.incident_id,
                        target_status=self._resolution_status,
                        expected_version=None,
                        correlation_id=wf_input.correlation_id,
                    ),
                    result_type=StatusProjectionActivityOutput,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=retry_policy,
                )
                self._status = proj_res.status
                self._status_version = proj_res.status_version

            return WorkflowStateSnapshot(
                incident_id=wf_input.incident_id,
                tenant_id=wf_input.tenant_id,
                status=self._status,
                status_version=self._status_version,
                cancel_requested=self._cancel_requested,
                hypothesis=self._hypothesis,
                observation_digests=list(self._observation_digests),
            )
        except asyncio.CancelledError:
            self._cancel_requested = True
            return await self._project_cancelled(wf_input, retry_policy)

    async def _run_stage3_execution(
        self,
        wf_input: WorkflowIncidentInput,
        exec_input: ApprovedExecutionSignalInput,
        retry_policy: RetryPolicy,
    ) -> WorkflowStateSnapshot:
        # 1. Transition AWAITING_APPROVAL -> EXECUTING
        proj_exec: StatusProjectionActivityOutput = await workflow.execute_activity(
            "b1_project_incident_status",
            StatusProjectionActivityInput(
                tenant_id=wf_input.tenant_id,
                incident_id=wf_input.incident_id,
                target_status=IncidentStatus.EXECUTING.value,
                expected_version=None,
                correlation_id=wf_input.correlation_id,
            ),
            result_type=StatusProjectionActivityOutput,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policy,
        )
        self._status = proj_exec.status
        self._status_version = proj_exec.status_version

        # 2. Execute B2 reservation + E8 conformance check + B3 receipt & ActionGateway
        dispatch_out: Stage3DispatchActivityOutput = await workflow.execute_activity(
            "stage3_execute_approved_action",
            exec_input,
            result_type=Stage3DispatchActivityOutput,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=retry_policy,
        )

        # If reconcile_pending during dispatch already verified the receipt:
        if dispatch_out.effect_state == "VERIFIED":
            proj_ver_step: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=IncidentStatus.VERIFYING.value,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_ver_step.status
            self._status_version = proj_ver_step.status_version

            proj_done: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=IncidentStatus.VERIFIED.value,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_done.status
            self._status_version = proj_done.status_version
        elif dispatch_out.effect_state == "CONFIRMED":
            # 3. Transition EXECUTING -> VERIFYING and run IndependentPostconditionVerifier
            proj_ver: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=IncidentStatus.VERIFYING.value,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_ver.status
            self._status_version = proj_ver.status_version

            ver_out: Stage3VerifyActivityOutput = await workflow.execute_activity(
                "stage3_verify_and_finalize_effect",
                dispatch_out,
                result_type=Stage3VerifyActivityOutput,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=retry_policy,
            )

            proj_final: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=ver_out.final_incident_status,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_final.status
            self._status_version = proj_final.status_version
        else:
            # FAILED or NEEDS_REVIEW directly from EXECUTING
            term_status = (
                IncidentStatus.NEEDS_REVIEW.value
                if dispatch_out.effect_state in ("NEEDS_REVIEW", "UNKNOWN")
                else IncidentStatus.FAILED.value
            )
            proj_fail: StatusProjectionActivityOutput = await workflow.execute_activity(
                "b1_project_incident_status",
                StatusProjectionActivityInput(
                    tenant_id=wf_input.tenant_id,
                    incident_id=wf_input.incident_id,
                    target_status=term_status,
                    expected_version=None,
                    correlation_id=wf_input.correlation_id,
                ),
                result_type=StatusProjectionActivityOutput,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policy,
            )
            self._status = proj_fail.status
            self._status_version = proj_fail.status_version

        return WorkflowStateSnapshot(
            incident_id=wf_input.incident_id,
            tenant_id=wf_input.tenant_id,
            status=self._status,
            status_version=self._status_version,
            cancel_requested=self._cancel_requested,
            hypothesis=self._hypothesis,
            observation_digests=list(self._observation_digests),
        )

    async def _project_cancelled(
        self, wf_input: WorkflowIncidentInput, retry_policy: RetryPolicy
    ) -> WorkflowStateSnapshot:
        proj_cancel: StatusProjectionActivityOutput = await workflow.execute_activity(
            "b1_project_incident_status",
            StatusProjectionActivityInput(
                tenant_id=wf_input.tenant_id,
                incident_id=wf_input.incident_id,
                target_status=IncidentStatus.CANCELLED.value,
                expected_version=None,
                correlation_id=wf_input.correlation_id,
            ),
            result_type=StatusProjectionActivityOutput,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policy,
        )
        self._status = proj_cancel.status
        self._status_version = proj_cancel.status_version
        return WorkflowStateSnapshot(
            incident_id=wf_input.incident_id,
            tenant_id=wf_input.tenant_id,
            status=self._status,
            status_version=self._status_version,
            cancel_requested=True,
            hypothesis=self._hypothesis,
            observation_digests=list(self._observation_digests),
        )
