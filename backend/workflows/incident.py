from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# Activities run outside replay and do all database / external I/O.
ACTIVITY_TIMEOUT = timedelta(seconds=15)
RETRIES = RetryPolicy(maximum_attempts=3)


@workflow.defn
class IncidentWorkflow:
    def __init__(self) -> None:
        self.cancel_requested = False
        self.review_acknowledged = False
        self.plan_decision: tuple[str, bool] | None = None

    @workflow.signal
    def request_cancel(self) -> None:
        self.cancel_requested = True

    @workflow.signal
    def acknowledge_review(self) -> None:
        self.review_acknowledged = True

    @workflow.signal
    def plan_decided(self, plan_id: str, approved: bool) -> None:
        self.plan_decision = (plan_id, approved)

    @workflow.run
    async def run(self, tenant_id: str, incident_id: str) -> str:
        async def project(state: str) -> None:
            await workflow.execute_activity(
                "project_incident_status",
                args=[tenant_id, incident_id, state],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRIES,
            )

        if self.cancel_requested:
            await project("CANCELLED")
            return "CANCELLED"

        await project("INVESTIGATING")
        route = await workflow.execute_activity(
            "select_incident_route",
            args=[tenant_id, incident_id],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRIES,
        )
        if route != "RUNBOOK":
            await project("NEEDS_REVIEW")
            return "NEEDS_REVIEW"
        probe_result = await workflow.execute_activity(
            "inspect_incident_read_only",
            args=[tenant_id, incident_id],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRIES,
        )
        if self.cancel_requested:
            await project("CANCELLED")
            return "CANCELLED"
        if probe_result != "SIMULATOR_REVIEW_REQUIRED":
            await project("NEEDS_REVIEW")
            return "NEEDS_REVIEW"

        # No unsupported production effect is performed by this baseline worker.
        await project("AWAITING_APPROVAL")
        await workflow.wait_condition(
            lambda: self.cancel_requested or self.review_acknowledged or self.plan_decision is not None
        )
        if self.cancel_requested:
            await project("CANCELLED")
            return "CANCELLED"
        if self.plan_decision is not None:
            plan_id, approved = self.plan_decision
            if not approved:
                await project("NEEDS_REVIEW")
                return "NEEDS_REVIEW"
            await project("EXECUTING")
            result = await workflow.execute_activity(
                "apply_approved_simulator_plan",
                args=[tenant_id, incident_id, plan_id],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=RETRIES,
            )
            final_state = "VERIFIED" if result == "VERIFIED" else "NEEDS_REVIEW"
            await project(final_state)
            return final_state
        await project("NEEDS_REVIEW")
        return "NEEDS_REVIEW"
