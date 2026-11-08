"""Deterministic Model Adapter for Stage 5 Adaptive Investigation."""

import re
import uuid
from typing import Any

from backend.domain.investigation import (
    HypothesisItem,
    InvestigationModelResponse,
    ModelAdapterProtocol,
    ProbeSelection,
    ProposedPlan,
)


class DeterministicFakeModelAdapter(ModelAdapterProtocol):
    """Deterministic model adapter implementing reproducible incident hypothesis formation.

    Recognizes simulator fault patterns (e.g. OOM, CPU saturation, network timeouts)
    and drives multi-step investigation loops.
    """

    def __init__(self, force_abstention: bool = False, force_probe_loop: bool = False) -> None:
        self.force_abstention = force_abstention
        self.force_probe_loop = force_probe_loop
        self._incident_call_counts: dict[uuid.UUID, int] = {}

    async def analyze_context(
        self,
        incident_id: uuid.UUID,
        rendered_context: str,
        tool_catalog_summary: list[dict[str, Any]],
    ) -> InvestigationModelResponse:
        """Evaluate context and return calibrated hypotheses, probe, or proposal."""
        count = self._incident_call_counts.get(incident_id, 0) + 1
        self._incident_call_counts[incident_id] = count

        if self.force_abstention:
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(
                        id="H1",
                        title="Unresolvable Inconclusive Fault",
                        probability=0.40,
                        rationale="Inconclusive telemetry",
                    ),
                    HypothesisItem(
                        id="H2",
                        title="Alternative Transient Anomaly",
                        probability=0.40,
                        rationale="Competing inconclusive indicators",
                    ),
                ],
                selected_probe=None,
                proposed_plan=None,
                rationale="Evidence is ambiguous and inconclusive; cannot discriminate further",
                tokens_used=600,
            )

        if self.force_probe_loop:
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(id="H1", title="Looping Anomaly", probability=0.50),
                    HypothesisItem(id="H2", title="Unknown Error", probability=0.50),
                ],
                selected_probe=ProbeSelection(
                    tool_id="telemetry_sample",
                    target_id="k8s:pod:order-service-9f",
                    parameters={"metric": "cpu"},
                ),
                tokens_used=500,
            )

        # Extract verified evidence observation IDs
        ev_matches = re.findall(r'<untrusted_observation id="([^"]+)"', rendered_context)
        evidence_ids = [uuid.UUID(m) for m in ev_matches]

        if not evidence_ids or count == 1:
            # Step 1: Initial hypotheses, select discriminating probe
            return InvestigationModelResponse(
                hypotheses=[
                    HypothesisItem(
                        id="H1",
                        title="OOM Killed Pod",
                        probability=0.50,
                        rationale="Initial alerts suggest potential memory starvation",
                    ),
                    HypothesisItem(
                        id="H2",
                        title="Upstream Network Partition",
                        probability=0.50,
                        rationale="Service mesh timeout symptoms",
                    ),
                ],
                selected_probe=ProbeSelection(
                    tool_id="telemetry_sample",
                    target_id="k8s:pod:order-service-9f",
                    parameters={"metric": "container_memory_working_set_bytes"},
                    rationale="Observe actual memory utilization to discriminate H1 vs H2",
                ),
                proposed_plan=None,
                rationale="Discriminating probe required to confirm OOM hypothesis",
                tokens_used=850,
            )

        # Step 2: Verified observations exist -> Propose repair plan
        return InvestigationModelResponse(
            hypotheses=[
                HypothesisItem(
                    id="H1",
                    title="OOM Killed Pod",
                    probability=0.92,
                    supporting_evidence_ids=evidence_ids,
                    rationale=f"Verified telemetry observations ({len(evidence_ids)} records) confirm memory exhausted",
                ),
                HypothesisItem(
                    id="H2",
                    title="Upstream Network Partition",
                    probability=0.08,
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
                supporting_evidence_ids=evidence_ids,
            ),
            rationale="High confidence OOM root cause confirmed by verified evidence ledger",
            tokens_used=950,
        )
