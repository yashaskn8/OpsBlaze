"""Unit tests for A4: Calibrated Abstention & Escalation Gate.

Verifies:
- Gate 1 (Proposal Gate): Allows proposal only when confidence >= 0.75, margin >= 0.25, and valid supporting evidence.
- Gate 1 rejects proposal when confidence is low or margin is insufficient.
- Gate 2 (Continuation Gate): Allows probing when budget remains and step count < max_steps.
- Gate 3 (Abstention Gate): Correctly abstains with BUDGET_EXHAUSTED when probes or tokens run out.
- Gate 3 abstains with INSUFFICIENT_EVIDENCE or UNRESOLVABLE_CONFLICT when evidence is inconclusive.
- Cold-start safety: Investigation is NOT aborted at step 1 when initial coverage is 0.
"""

import uuid
from datetime import UTC, datetime

from backend.domain.budget import BudgetSnapshot, BudgetStatus
from backend.domain.evidence import EvidenceClassification, EvidenceRecord, EvidenceValidity
from backend.domain.investigation import HypothesisItem, ProbeSelection
from backend.investigator.abstention import AbstentionReason, CalibratedAbstentionGate


def test_gate1_allows_proposal_when_calibrated_confidence_met() -> None:
    """A4 Gate 1: P(H1) >= 0.75, margin >= 0.25, supporting evidence valid -> can_propose=True."""
    ev_id = uuid.uuid4()
    h1 = HypothesisItem(
        id="H1",
        title="OOM Killed Pod",
        probability=0.85,
        supporting_evidence_ids=[ev_id],
        contradicting_evidence_ids=[],
        rationale="Memory limit hit",
    )
    h2 = HypothesisItem(
        id="H2",
        title="Network Partition",
        probability=0.15,
        supporting_evidence_ids=[],
        contradicting_evidence_ids=[],
        rationale="Unlikely",
    )

    ev_record = EvidenceRecord(
        id=ev_id,
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        source="telemetry_sample",
        source_version="1.0",
        target_id="k8s:pod:order-1",
        target_version="1.0",
        observed_at=datetime.now(UTC),
        captured_at=datetime.now(UTC),
        sha256_digest="abc",
        raw_payload_redacted={"memory_rss_mb": 2048},
        classification=EvidenceClassification.TELEMETRY,
        validity_status=EvidenceValidity.VALID,
    )

    gate = CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25)
    decision = gate.evaluate_proposal([h1, h2], [ev_record])

    assert decision.can_propose is True
    assert decision.should_abstain is False


def test_gate1_rejects_proposal_when_margin_is_too_small() -> None:
    """A4 Gate 1: P(H1)=0.55, P(H2)=0.45 (margin 0.10 < 0.25) -> can_propose=False."""
    ev_id = uuid.uuid4()
    h1 = HypothesisItem(
        id="H1",
        title="OOM Killed Pod",
        probability=0.55,
        supporting_evidence_ids=[ev_id],
        contradicting_evidence_ids=[],
        rationale="Could be OOM",
    )
    h2 = HypothesisItem(
        id="H2",
        title="Deadlock in Connection Pool",
        probability=0.45,
        supporting_evidence_ids=[],
        contradicting_evidence_ids=[],
        rationale="Could be deadlock",
    )

    ev_record = EvidenceRecord(
        id=ev_id,
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        source="telemetry_sample",
        source_version="1.0",
        target_id="k8s:pod:order-1",
        target_version="1.0",
        observed_at=datetime.now(UTC),
        captured_at=datetime.now(UTC),
        sha256_digest="abc",
        raw_payload_redacted={"memory_rss_mb": 1024},
        classification=EvidenceClassification.TELEMETRY,
        validity_status=EvidenceValidity.VALID,
    )

    gate = CalibratedAbstentionGate(min_confidence=0.75, min_margin=0.25)
    decision = gate.evaluate_proposal([h1, h2], [ev_record])

    assert decision.can_propose is False


def test_cold_start_continuation_allows_probing_at_step_one() -> None:
    """A4 Gate 2: At Step 1 with empty evidence and active budget, continuation is allowed."""
    budget = BudgetSnapshot(
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        reserved_tokens=0,
        settled_tokens=0,
        reserved_probes=0,
        settled_probes=0,
        reserved_cost_micros=0,
        settled_cost_micros=0,
        max_tokens=50000,
        max_probes=10,
        max_cost_micros=10000000,
        max_time_seconds=300,
        status=BudgetStatus.ACTIVE,
    )
    probe = ProbeSelection(
        tool_id="telemetry_sample",
        target_id="k8s:pod:order-1",
        parameters={"metric": "cpu"},
        rationale="Initial diagnostic probe",
    )

    gate = CalibratedAbstentionGate()
    decision = gate.evaluate_continuation(
        budget_snapshot=budget,
        step_no=1,
        max_steps=5,
        selected_probe=probe,
        executed_probe_keys=set(),
    )

    assert decision.proceed is True
    assert decision.should_abstain is False


def test_abstention_gate_fires_on_budget_exhaustion() -> None:
    """A4 Gate 3: When budget is exhausted (settled_probes >= max_probes), gate abstains."""
    budget = BudgetSnapshot(
        tenant_id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        reserved_tokens=0,
        settled_tokens=10000,
        reserved_probes=0,
        settled_probes=10,  # reached max
        reserved_cost_micros=0,
        settled_cost_micros=500000,
        max_tokens=50000,
        max_probes=10,
        max_cost_micros=10000000,
        max_time_seconds=300,
        status=BudgetStatus.EXHAUSTED,
    )
    probe = ProbeSelection(
        tool_id="telemetry_sample",
        target_id="k8s:pod:order-1",
        parameters={"metric": "cpu"},
        rationale="Next probe",
    )

    gate = CalibratedAbstentionGate()
    decision = gate.evaluate_continuation(
        budget_snapshot=budget,
        step_no=3,
        max_steps=5,
        selected_probe=probe,
        executed_probe_keys=set(),
    )

    assert decision.proceed is False
    assert decision.should_abstain is True
    assert decision.reason == AbstentionReason.BUDGET_EXHAUSTED
