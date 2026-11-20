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

    @workflow.signal
    def request_cancel(self) -> None:
        self.cancel_requested = True

    @workflow.signal
    def acknowledge_review(self) -> None:
        self.review_acknowledged = True

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
        await workflow.execute_activity(
            "inspect_incident_read_only",
            args=[tenant_id, incident_id],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRIES,
        )
        if self.cancel_requested:
            await project("CANCELLED")
            return "CANCELLED"

        # No unsupported production effect is performed by this baseline worker.
        await project("AWAITING_APPROVAL")
        await workflow.wait_condition(
            lambda: self.cancel_requested or self.review_acknowledged
        )
        if self.cancel_requested:
            await project("CANCELLED")
            return "CANCELLED"
        await project("NEEDS_REVIEW")
        return "NEEDS_REVIEW"
