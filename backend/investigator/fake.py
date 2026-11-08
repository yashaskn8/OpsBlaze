"""Deterministic fake read-only investigator for OpsBlaze B1."""

from __future__ import annotations

import hashlib

from backend.domain.incident import (
    IncidentStatus,
    InvestigationObservation,
    InvestigationResult,
    WorkflowIncidentInput,
)


class FakeInvestigator:
    """Deterministic fixture-based read-only investigator.

    Guarantees:
    - Accepts a typed WorkflowIncidentInput reference.
    - Produces a typed InvestigationResult with deterministic fixture observations.
    - Performs zero external network activity, zero shell commands, and zero mutations.
    - Does not claim or pretend to invoke an LLM.
    """

    def investigate(self, wf_input: WorkflowIncidentInput) -> InvestigationResult:
        seed_material = f"{wf_input.tenant_id}:{wf_input.incident_id}:{wf_input.target_id}"
        digest_1 = hashlib.sha256(f"{seed_material}:probe-health".encode()).hexdigest()[:16]
        digest_2 = hashlib.sha256(f"{seed_material}:probe-metrics".encode()).hexdigest()[:16]

        obs_1 = InvestigationObservation(
            probe_id="b1.fixture.readonly.target_status",
            target_id=wf_input.target_id,
            finding_code="SIM_DEGRADED_LATENCY",
            summary=(
                f"Deterministic fixture probe observed elevated read latency on "
                f"target '{wf_input.target_id}' (read-only snapshot)."
            ),
            read_only=True,
            deterministic_digest=digest_1,
        )
        obs_2 = InvestigationObservation(
            probe_id="b1.fixture.readonly.error_budget",
            target_id=wf_input.target_id,
            finding_code="SIM_REPLICA_LAG_DETECTED",
            summary=(
                f"Deterministic fixture probe confirmed replica lag exceeds threshold on "
                f"target '{wf_input.target_id}'; human approval required before any action."
            ),
            read_only=True,
            deterministic_digest=digest_2,
        )

        return InvestigationResult(
            incident_id=wf_input.incident_id,
            tenant_id=wf_input.tenant_id,
            target_id=wf_input.target_id,
            hypothesis=(
                f"Deterministic B1 fixture diagnosis for target '{wf_input.target_id}': "
                f"read-only indicators suggest replica lag; awaiting human approval."
            ),
            recommended_status=IncidentStatus.AWAITING_APPROVAL,
            requires_human_approval=True,
            observations=[obs_1, obs_2],
        )
