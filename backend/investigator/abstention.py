"""A4 Calibrated Abstention & Escalation Gate."""

from enum import StrEnum

from pydantic import BaseModel

from backend.domain.budget import BudgetSnapshot, BudgetStatus
from backend.domain.evidence import EvidenceRecord
from backend.domain.investigation import HypothesisItem, ProbeSelection


class AbstentionReason(StrEnum):
    """Calibrated, attributable rationale for escalating to human responders."""

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    UNRESOLVABLE_CONFLICT = "UNRESOLVABLE_CONFLICT"
    NO_DISCRIMINATING_PROBE = "NO_DISCRIMINATING_PROBE"
    CONFIDENCE_MARGIN_INSUFFICIENT = "CONFIDENCE_MARGIN_INSUFFICIENT"
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"


class GateDecision(BaseModel):
    """Tri-state outcome of an abstention gate evaluation."""

    proceed: bool = False
    can_propose: bool = False
    should_abstain: bool = False
    reason: AbstentionReason | None = None
    explanation: str = ""


class CalibratedAbstentionGate:
    """Evaluates three decoupled gates: Proposal Gate, Continuation Gate, and Abstention Gate."""

    def __init__(self, min_confidence: float = 0.75, min_margin: float = 0.25) -> None:
        self.min_confidence = min_confidence
        self.min_margin = min_margin

    def evaluate_proposal(
        self,
        hypotheses: list[HypothesisItem],
        valid_evidence: list[EvidenceRecord],
    ) -> GateDecision:
        """Gate 1: Verifies whether confidence, margin, and valid evidence justify plan proposal."""
        if not hypotheses:
            return GateDecision(can_propose=False, explanation="No hypotheses provided")

        sorted_h = sorted(hypotheses, key=lambda h: h.probability, reverse=True)
        h1 = sorted_h[0]

        if h1.probability < self.min_confidence:
            return GateDecision(
                can_propose=False,
                explanation=f"Top hypothesis confidence {h1.probability:.2f} < threshold {self.min_confidence:.2f}",
            )

        runner_up = sorted_h[1].probability if len(sorted_h) > 1 else 0.0
        margin = h1.probability - runner_up
        if margin < self.min_margin:
            return GateDecision(
                can_propose=False,
                explanation=f"Confidence margin {margin:.2f} < threshold {self.min_margin:.2f}",
            )

        valid_ids = {e.id for e in valid_evidence}
        if not h1.supporting_evidence_ids or not any(
            eid in valid_ids for eid in h1.supporting_evidence_ids
        ):
            return GateDecision(
                can_propose=False,
                explanation="Top hypothesis lacks verified supporting evidence in current context",
            )

        for eid in h1.contradicting_evidence_ids:
            if eid in valid_ids:
                return GateDecision(
                    can_propose=False,
                    explanation=f"Top hypothesis contradicted by active evidence record {eid}",
                )

        return GateDecision(
            can_propose=True,
            explanation="Confidence and margin thresholds satisfied with verified evidence",
        )

    def evaluate_continuation(
        self,
        budget_snapshot: BudgetSnapshot,
        step_no: int,
        max_steps: int,
        selected_probe: ProbeSelection | None,
        executed_probe_keys: set[str],
    ) -> GateDecision:
        """Gate 2 & Gate 3: Determines whether investigation can continue or must abstain."""
        # Budget check
        if (
            budget_snapshot.status == BudgetStatus.EXHAUSTED
            or budget_snapshot.settled_probes >= budget_snapshot.max_probes
            or budget_snapshot.settled_tokens >= budget_snapshot.max_tokens
            or budget_snapshot.settled_cost_micros >= budget_snapshot.max_cost_micros
        ):
            return GateDecision(
                proceed=False,
                should_abstain=True,
                reason=AbstentionReason.BUDGET_EXHAUSTED,
                explanation="Investigation budget limit reached",
            )

        # Step count check
        if step_no > max_steps:
            return GateDecision(
                proceed=False,
                should_abstain=True,
                reason=AbstentionReason.MAX_STEPS_EXCEEDED,
                explanation=f"Step {step_no} exceeds max investigation steps {max_steps}",
            )

        # Probe check
        if selected_probe is None:
            return GateDecision(
                proceed=False,
                should_abstain=True,
                reason=AbstentionReason.NO_DISCRIMINATING_PROBE,
                explanation="Model did not select a discriminating probe",
            )

        probe_key = (
            f"{selected_probe.tool_id}:{selected_probe.target_id}:"
            f"{sorted(selected_probe.parameters.items())}"
        )
        if probe_key in executed_probe_keys:
            return GateDecision(
                proceed=False,
                should_abstain=True,
                reason=AbstentionReason.UNRESOLVABLE_CONFLICT,
                explanation="Probe loop detected: repeating identical probe without progress",
            )

        return GateDecision(
            proceed=True,
            should_abstain=False,
            explanation="Continuation allowed",
        )
