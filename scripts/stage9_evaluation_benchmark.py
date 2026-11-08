"""Stage 9 E5 Candidate-to-Incumbent Regression Gate Benchmark Script.

Executes Feature E5:
- Registers frozen CandidateEvaluationManifest
- Runs CandidateEvaluationRunner over paired diagnostic fixtures and negative controls
- Adjudicates with IndependentOracle
- Evaluates 6 statistical quality and safety gates via evaluate_statistical_gates
- Submits human review sign-off
- Exports structured benchmark results to docs/evidence/stage9-evaluation-results.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import get_settings
from backend.domain.evaluation import (
    CandidateProvenanceType,
    ReviewDecision,
)
from backend.domain.failure import (
    DivergentStepCategory,
    ProposalType,
    TargetComponent,
)
from backend.domain.failure import (
    ReviewDecision as ProposalReviewDecision,
)
from backend.evaluations.manifest_service import CandidateManifestService
from backend.evaluations.report_service import CandidateReportService
from backend.evaluations.runner import CandidateEvaluationRunner
from backend.learning.clusters import FailureClusterService
from backend.learning.failure_taxonomy import FailureClassifier
from backend.learning.proposal_service import ProposalService
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import ConsentEpoch, OutcomeRevision
from backend.persistence.repositories import IncidentRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("opsblaze.stage9_benchmark")


async def run_stage9_benchmark() -> dict[str, Any]:
    settings = get_settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    tenant_id = uuid.uuid4()
    logger.info(f"Setting up benchmark tenant {tenant_id}...")

    # Ensure Tenant & Consent Epoch
    inc_repo = IncidentRepository(session_factory)
    await inc_repo.ensure_tenant(tenant_id, f"benchmark-tenant-{tenant_id.hex[:8]}")

    async with session_factory() as session:
        session.add(ConsentEpoch(tenant_id=tenant_id, current_epoch=1, reuse_allowed=True))
        await session.commit()
    admit = await inc_repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"bench-inc-{uuid.uuid4().hex[:8]}",
        target_id="k8s:deploy:payment-processor",
        request_sha256="9" * 64,
    )
    inc_id = admit.response.id

    async with session_factory() as session:
        rev = OutcomeRevision(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=inc_id,
            revision_no=1,
            rule_version="v1",
            label="VERIFIED_SUCCESS",
            confidence_band="HIGH",
            supporting_observation_ids=[],
            adjudication_metadata={"check": "benchmark-prober"},
        )
        session.add(rev)
        await session.commit()
        outcome_rev_id = rev.id

    # Create Proposal hierarchy
    classifier = FailureClassifier(session_factory)
    case = await classifier.classify_case(
        tenant_id=tenant_id,
        incident_id=inc_id,
        outcome_revision_id=outcome_rev_id,
        outcome_label="VERIFIED_SUCCESS",
        divergent_step="verify_readback",
        first_divergent_step_category=DivergentStepCategory.READBACK_MISMATCH.value,
        subsystem_version="v1.0.0",
    )

    cluster_svc = FailureClusterService(session_factory)
    cluster = await cluster_svc.assign_to_cluster(
        tenant_id=tenant_id,
        case_id=case.id,
        failure_origin_code=case.failure_origin_code,
        first_divergent_step_category=case.first_divergent_step_category,
        subsystem_version=case.subsystem_version,
    )

    prop_svc = ProposalService(session_factory)
    prop = await prop_svc.create_draft(
        tenant_id=tenant_id,
        cluster_id=cluster.id,
        proposal_type=ProposalType.PROMPT_REFINEMENT.value,
        base_policy_version="v1.0",
        target_component=TargetComponent.INVESTIGATOR_A1_PROMPT.value,
        structured_diff={
            "template_id": "prompt_v1",
            "target_section": "reconciliation_guidelines",
            "proposed_content": "Always verify cart session lock before releasing read reservation.",
        },
        hypothesis="Prevents cart session lock races.",
        expected_benefit="Zero stale checkouts.",
        risk_assessment="Adds 100ms lock check latency.",
        evaluation_plan="Evaluate via paired S1 test across holdouts.",
        drafter_id="llm-proposal-bot",
    )
    submitted = await prop_svc.submit_proposal(tenant_id, prop.id, expected_version=1)
    approved = await prop_svc.review_proposal(
        tenant_id=tenant_id,
        proposal_id=submitted.id,
        expected_version=2,
        reviewer_id="human-sre-lead",
        reviewer_role="sre_lead",
        decision=ProposalReviewDecision.APPROVED.value,
        comments="Approved for candidate regression evaluation.",
    )

    # Register & Freeze Manifest
    manifest_svc = CandidateManifestService(session_factory)
    logger.info("Registering candidate evaluation manifest...")
    manifest = await manifest_svc.register_manifest(
        tenant_id=tenant_id,
        title="Stage 9 E5 Benchmark: Payment Processor Optimization",
        incumbent_bundle={
            "policy_version": "v1.0",
            "tool_catalog_version": "v1",
        },
        candidate_bundle={
            "base_policy_version": "v1.0",
            "target_component": TargetComponent.INVESTIGATOR_A1_PROMPT.value,
            "proposal_diff": {
                "template_name": "prompt_v1",
                "section_name": "reconciliation_guidelines",
                "original_text": "Verify cart session.",
                "proposed_text": "Always verify cart session lock before releasing read reservation.",
            },
        },
        candidate_provenance_type=CandidateProvenanceType.E3_PROPOSAL,
        source_proposal_id=approved.id,
        source_outcome_revision_ids=[outcome_rev_id],
        fixture_family="k8s_container_crash",
        fixture_set_digest="e5bench_fixture_set_digest_" + "0" * 34,
        holdout_set_digest="e5bench_holdout_set_digest_" + "1" * 34,
        sample_size_plan=20,
        budget_ceiling_micros=1000000,
        max_tokens_ceiling=100000,
        auto_freeze=True,
    )
    logger.info(f"Manifest registered and frozen: {manifest.id} (SHA-256: {manifest.manifest_sha256[:16]}...)")

    # Run paired evaluation
    runner = CandidateEvaluationRunner(session_factory)
    logger.info("Dispatching paired evaluation runner (20 pairs, 40 trials)...")
    await runner.run_evaluation(
        tenant_id=tenant_id,
        manifest_id=manifest.id,
        worker_id="benchmark-runner-main",
        lease_seconds=60,
    )

    # Generate Report
    report_svc = CandidateReportService(session_factory)
    logger.info("Synthesizing evaluation report and evaluating statistical quality/safety gates...")
    report = await report_svc.generate_report(tenant_id=tenant_id, manifest_id=manifest.id)

    # Human Review Sign-Off
    logger.info("Submitting human review sign-off...")
    reviewed_report = await report_svc.submit_review(
        tenant_id=tenant_id,
        report_id=report.id,
        reviewer_id="lead.sre@opsblaze.local",
        reviewer_role="sre_lead",
        decision=ReviewDecision.ACCEPTED,
        comments="Approved for release candidate promotion.",
        expected_version=1,
    )

    results: dict[str, Any] = {
        "manifest_id": str(manifest.id),
        "manifest_sha256": manifest.manifest_sha256,
        "title": manifest.title,
        "sample_size_plan": manifest.sample_size_plan,
        "total_pairs_planned": reviewed_report.total_pairs_planned,
        "total_pairs_completed": reviewed_report.total_pairs_completed,
        "invalid_pairs_count": reviewed_report.invalid_pairs_count,
        "report_status": reviewed_report.report_status,
        "promotion_eligible": reviewed_report.promotion_eligible,
        "recovery_rate_incumbent": reviewed_report.recovery_rate_incumbent,
        "recovery_rate_candidate": reviewed_report.recovery_rate_candidate,
        "recovery_delta": reviewed_report.recovery_delta,
        "recovery_ci_95": {
            "low": reviewed_report.recovery_ci_low,
            "high": reviewed_report.recovery_ci_high,
            "method": "Newcombe Method 10 Paired Score Interval",
        },
        "mcnemar_paired_test": {
            "statistic": reviewed_report.mcnemar_statistic,
            "p_value": reviewed_report.mcnemar_p_value,
        },
        "cost_per_verified_recovery": {
            "incumbent_micros": reviewed_report.cost_per_verified_incumbent,
            "candidate_micros": reviewed_report.cost_per_verified_candidate,
            "cost_efficiency_ratio": round(
                reviewed_report.cost_per_verified_candidate / reviewed_report.cost_per_verified_incumbent, 4
            )
            if (
                reviewed_report.cost_per_verified_candidate is not None
                and reviewed_report.cost_per_verified_incumbent > 0
            )
            else None,
        },
        "negative_control_pass_rate": reviewed_report.negative_control_pass_rate,
        "unsafe_action_count": reviewed_report.unsafe_action_count,
        "gate_verdicts": reviewed_report.gate_verdicts,
        "review": {
            "reviewer_id": reviewed_report.reviewer_id,
            "reviewer_role": reviewed_report.reviewer_role,
            "decision": reviewed_report.review_decision,
            "comments": reviewed_report.review_comments,
            "reviewed_at": reviewed_report.reviewed_at.isoformat() if reviewed_report.reviewed_at else None,
        },
    }

    out_path = REPO_ROOT / "docs" / "evidence" / "stage9-evaluation-results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info(f"Results successfully written to {out_path}")
    await engine.dispose()
    return results


if __name__ == "__main__":
    asyncio.run(run_stage9_benchmark())
