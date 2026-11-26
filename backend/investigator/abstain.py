
"""A4 deterministic hard gates and development-only ordinal threshold calibration."""

from dataclasses import asdict, dataclass
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.simulator import utc
from backend.models import (
    DecisionAssessment, Evidence, Incident, ProbeCall, SimulatorTarget,
)
from backend.probes.gateway import evidence_links_intact
from backend.settings import get_settings

FEATURE_VERSION = "simulator-evidence-v1"
THRESHOLD_VERSION = "UNCALIBRATED-hard-gates-v1"
CALIBRATION_SCORE_KIND = "uncalibrated_ordinal_risk_score"


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    family: str
    expected: Literal["PROPOSE", "ABSTAIN"]
    evidence_age_seconds: int
    probe_denials: int
    version_drift: bool
    disputed_history: bool
    missing_evidence: bool


DEFAULT_DEV_CALIBRATION_FIXTURES: tuple[CalibrationCase, ...] = (
    CalibrationCase("dev-1", "dev-recoverable", "PROPOSE", 15, 0, False, False, False),
    CalibrationCase("dev-2", "dev-recoverable", "PROPOSE", 45, 0, False, False, False),
    CalibrationCase("dev-3", "dev-recoverable", "PROPOSE", 120, 0, False, False, False),
    CalibrationCase("dev-4", "dev-drift", "ABSTAIN", 20, 0, True, False, False),
    CalibrationCase("dev-5", "dev-stale", "ABSTAIN", 600, 0, False, False, False),
    CalibrationCase("dev-6", "dev-missing", "ABSTAIN", 0, 1, False, False, True),
)

DEFAULT_EVAL_CALIBRATION_FIXTURES: tuple[CalibrationCase, ...] = (
    CalibrationCase("eval-1", "eval-fault-a", "PROPOSE", 10, 0, False, False, False),
    CalibrationCase("eval-2", "eval-fault-a", "PROPOSE", 90, 0, False, False, False),
    CalibrationCase("eval-3", "eval-fault-b", "PROPOSE", 60, 0, False, False, False),
    CalibrationCase("eval-4", "eval-disputed", "ABSTAIN", 30, 0, False, True, False),
    CalibrationCase("eval-5", "eval-expired", "ABSTAIN", 900, 0, False, False, False),
    CalibrationCase("eval-6", "eval-denied", "ABSTAIN", 25, 1, False, False, False),
)


def compute_abstention_risk_score(case: CalibrationCase) -> int:
    """Compute a bounded ordinal risk score (0-100); NOT a calibrated probability."""
    score = 0
    if case.evidence_age_seconds > 300:
        score += 35
    if case.probe_denials > 0:
        score += 35
    if case.version_drift:
        score += 40
    if case.disputed_history:
        score += 35
    if case.missing_evidence:
        score += 50
    return min(100, score)


def _score_threshold(cases: list[CalibrationCase], threshold: int) -> dict:
    proposed = 0
    abstained = 0
    unsafe = 0
    for case in cases:
        score = compute_abstention_risk_score(case)
        decision = "PROPOSE" if score <= threshold else "ABSTAIN"
        if decision == "PROPOSE":
            proposed += 1
            if case.expected == "ABSTAIN":
                unsafe += 1
        else:
            abstained += 1
    total = len(cases)
    coverage = round(proposed / total, 4) if total else 0.0
    selective_risk = round(unsafe / proposed, 4) if proposed else 0.0
    unsafe_rate = round(unsafe / total, 4) if total else 0.0
    return {
        "threshold": threshold,
        "pairs": total,
        "proposed": proposed,
        "abstained": abstained,
        "coverage": coverage,
        "selective_risk": selective_risk,
        "unsafe_proposals": unsafe,
        "unsafe_rate": unsafe_rate,
    }


def freeze_dev_threshold(
    dev_cases: list[CalibrationCase],
    *,
    max_unsafe_rate: float = 0.0,
) -> dict:
    """A4-06: select and freeze an ordinal threshold exclusively on development cases."""
    if not dev_cases:
        raise ValueError("development calibration corpus must not be empty")
    if not 0.0 <= max_unsafe_rate <= 1.0:
        raise ValueError("max_unsafe_rate must be between 0 and 1")
    dev_ids = [case.case_id for case in dev_cases]
    if len(set(dev_ids)) != len(dev_ids):
        raise ValueError("duplicate development calibration case_id")
    dev_families = sorted({case.family for case in dev_cases})
    best: dict | None = None
    for candidate in range(0, 101, 5):
        summary = _score_threshold(dev_cases, candidate)
        if summary["unsafe_rate"] <= max_unsafe_rate and (
            best is None
            or summary["coverage"] > best["coverage"]
            or (
                summary["coverage"] == best["coverage"]
                and summary["threshold"] < best["threshold"]
            )
        ):
            best = summary
    if best is None:
        best = _score_threshold(dev_cases, 0)
    dev_digest = hashlib.sha256(
        json.dumps([asdict(c) for c in dev_cases], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "threshold": best["threshold"],
        "score_kind": CALIBRATION_SCORE_KIND,
        "is_calibrated_probability": False,
        "promotion_eligible": False,
        "dev_digest": dev_digest,
        "dev_case_ids": sorted(dev_ids),
        "dev_families": dev_families,
        "dev_metrics": best,
    }


def evaluate_frozen_threshold(
    frozen: dict,
    eval_cases: list[CalibrationCase],
) -> dict:
    """A4-06: evaluate frozen threshold on strictly disjoint evaluation families/cases."""
    if not eval_cases:
        raise ValueError("evaluation calibration corpus must not be empty")
    eval_ids = {case.case_id for case in eval_cases}
    if len(eval_ids) != len(eval_cases):
        raise ValueError("duplicate evaluation calibration case_id")
    eval_families = {case.family for case in eval_cases}
    if eval_ids & set(frozen["dev_case_ids"]) or eval_families & set(frozen["dev_families"]):
        raise ValueError("development and evaluation split contamination detected")

    threshold = int(frozen["threshold"])
    overall = _score_threshold(eval_cases, threshold)
    by_family: dict[str, dict] = {}
    for family in sorted(eval_families):
        subset = [case for case in eval_cases if case.family == family]
        by_family[family] = _score_threshold(subset, threshold)

    curve = [_score_threshold(eval_cases, cut) for cut in (10, 25, 40, 60, 80)]
    eval_digest = hashlib.sha256(
        json.dumps([asdict(c) for c in eval_cases], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "threshold": threshold,
        "score_kind": CALIBRATION_SCORE_KIND,
        "is_calibrated_probability": False,
        "promotion_eligible": False,
        "dev_digest": frozen["dev_digest"],
        "eval_digest": eval_digest,
        "overall": overall,
        "family_metrics": by_family,
        "risk_coverage_curve": curve,
    }


async def accepted_snapshot_is_still_safe(
    session: AsyncSession, incident: Incident, tenant_id: str,
    evidence_ids: list[str],
) -> bool:
    """Never replay a stale A4 positive after later probes or source edits."""
    if (
        len(evidence_ids) != 2
        or len(set(evidence_ids)) != 2
        or incident.probe_used > get_settings().max_probe_calls_per_incident
    ):
        return False
    try:
        refs = [UUID(item) for item in evidence_ids]
    except (TypeError, ValueError, AttributeError):
        return False
    if not await evidence_links_intact(session, incident.id, refs):
        return False
    denied = await session.scalar(
        select(ProbeCall.id).where(
            ProbeCall.incident_id == incident.id,
            ProbeCall.outcome == "DENIED",
        ).limit(1)
    )
    if denied is not None:
        return False
    target = await session.get(
        SimulatorTarget, (tenant_id, incident.target_id)
    )
    if target is None or target.healthy:
        return False
    rows = (await session.scalars(
        select(Evidence).where(
            Evidence.tenant_id == tenant_id,
            Evidence.incident_id == incident.id,
            Evidence.id.in_(refs),
        )
    )).all()
    if len(rows) != 2 or {row.source for row in rows} != {
        "simulator.health", "simulator.version",
    }:
        return False
    now = datetime.now(timezone.utc)
    if any(
        row.validity != "VALID"
        or (row.expires_at is not None and utc(row.expires_at) <= now)
        or row.target_id != incident.target_id
        or row.target_version != target.version
        or row.source_version != "simulator-v1"
        or row.classification != "INTERNAL"
        or not timedelta(0) <= now - utc(row.observed_at) <= timedelta(minutes=5)
        or hashlib.sha256(row.redacted_excerpt.encode()).hexdigest() != row.sha256
        for row in rows
    ):
        return False
    observed = {row.source: row.redacted_excerpt for row in rows}
    return (
        observed["simulator.health"] == '{"healthy":false}'
        and observed["simulator.version"] == json.dumps(
            {"version": target.version}, sort_keys=True, separators=(",", ":"),
        )
    )


async def assess_decision(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: UUID,
    step_no: int, evidence_ids: list[str],
    *, handoff_reason: Literal[
        "TOOL_BLOCKED", "TOOL_UNAVAILABLE", "MODEL_UNCERTAINTY", "TARGET_ALREADY_HEALTHY",
    ] | None = None,
) -> DecisionAssessment | None:
    if step_no < 1:
        raise ValueError("assessment step must be positive")
    if handoff_reason is not None and handoff_reason not in {
        "TOOL_BLOCKED", "TOOL_UNAVAILABLE", "MODEL_UNCERTAINTY", "TARGET_ALREADY_HEALTHY",
    }:
        raise ValueError("invalid assessment handoff reason")
    canonical = json.dumps(evidence_ids, separators=(",", ":"), sort_keys=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()

    async with sessions() as session:
        async with session.begin():
            incident = await session.scalar(
                select(Incident).where(
                    Incident.tenant_id == tenant_id,
                    Incident.id == incident_id,
                ).with_for_update()
            )
            if incident is None:
                return None
            existing = await session.scalar(
                select(DecisionAssessment).where(
                    DecisionAssessment.incident_id == incident_id,
                    DecisionAssessment.step_no == step_no,
                )
            )
            if existing is not None:
                # An old positive decision is never reusable for a new snapshot.
                # Keep the durable assessment immutable; the caller must abstain.
                if existing.evidence_digest != digest:
                    return None
                if existing.outcome == "PROPOSED" and (
                    handoff_reason is not None or not await accepted_snapshot_is_still_safe(
                        session, incident, tenant_id, evidence_ids,
                    )
                ):
                    return None
                return existing

            reason = "ACCEPTED"
            if handoff_reason is not None:
                reason = handoff_reason
                if (
                    handoff_reason == "TOOL_BLOCKED"
                    and step_no == incident.probe_used + 1
                    and incident.probe_used >= get_settings().max_probe_calls_per_incident
                ):
                    reason = "BUDGET_EXHAUSTED"
            elif len(evidence_ids) != 2 or len(set(evidence_ids)) != 2:
                reason = "MISSING_EVIDENCE"
            elif incident.probe_used > get_settings().max_probe_calls_per_incident:
                reason = "BUDGET_EXHAUSTED"
            else:
                probes = (await session.scalars(
                    select(ProbeCall).where(
                        ProbeCall.incident_id == incident_id,
                        ProbeCall.outcome == "DENIED",
                    ).limit(1)
                )).first()
                if probes is not None:
                    reason = "TOOL_BLOCKED"
                else:
                    try:
                        refs = [UUID(value) for value in evidence_ids]
                    except (ValueError, TypeError):
                        reason = "INVALID_REFERENCE"
                    else:
                        evidence = (await session.scalars(
                            select(Evidence).where(
                                Evidence.tenant_id == tenant_id,
                                Evidence.incident_id == incident_id,
                                Evidence.id.in_(refs),
                            )
                        )).all()
                        sources = {row.source for row in evidence}
                        now = datetime.now(timezone.utc)
                        if (
                            len(evidence) == 2
                            and any(
                                row.validity == "EXPIRED"
                                or (row.expires_at is not None and utc(row.expires_at) <= now)
                                for row in evidence
                            )
                        ):
                            reason = "EXPIRED_EVIDENCE"
                        elif (
                            len(evidence) != 2
                            or sources != {"simulator.health", "simulator.version"}
                            or any(
                                row.validity != "VALID"
                                or row.source_version != "simulator-v1"
                                or row.classification != "INTERNAL"
                                for row in evidence
                            )
                        ):
                            reason = "INVALID_EVIDENCE"
                        elif any(
                            hashlib.sha256(row.redacted_excerpt.encode()).hexdigest()
                            != row.sha256
                            for row in evidence
                        ):
                            reason = "HASH_MISMATCH"
                        elif any(
                            not timedelta(0)
                            <= datetime.now(timezone.utc) - utc(row.observed_at)
                            <= timedelta(minutes=5)
                            for row in evidence
                        ):
                            reason = "STALE_EVIDENCE"
                        elif not await evidence_links_intact(
                            session, incident_id, refs
                        ):
                            reason = "UNLINKED_EVIDENCE"
                        else:
                            target = await session.get(
                                SimulatorTarget, (tenant_id, incident.target_id)
                            )
                            versions = {row.target_version for row in evidence}
                            if (
                                target is None or len(versions) != 1
                                or target.version not in versions
                                or any(row.target_id != incident.target_id for row in evidence)
                            ):
                                reason = "VERSION_CHANGED"
                            elif target.healthy:
                                reason = "TARGET_ALREADY_HEALTHY"
                            else:
                                observations = {
                                    row.source: row.redacted_excerpt for row in evidence
                                }
                                if (
                                    observations["simulator.health"] != '{"healthy":false}'
                                    or observations["simulator.version"] != json.dumps(
                                        {"version": target.version},
                                        sort_keys=True, separators=(",", ":"),
                                    )
                                ):
                                    reason = "INVALID_OBSERVATION"

            assessment = DecisionAssessment(
                id=uuid4(), tenant_id=tenant_id, incident_id=incident_id,
                step_no=step_no, feature_version=FEATURE_VERSION,
                threshold_version=THRESHOLD_VERSION,
                evidence_digest=digest,
                risk_score=0 if reason == "ACCEPTED" else 100,
                reason=reason,
                outcome="PROPOSED" if reason == "ACCEPTED" else "ABSTAINED",
            )
            session.add(assessment)
        return assessment
