
"""E5 small paired non-regression gate; results cannot authorize production."""

import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.evaluation.fixtures import (
    FIXTURES, INCUMBENT, ORACLE_VERSION, candidate_config,
    evaluate_strategy, fixture_digest, oracle_expected,
)
from backend.learning.failures import validate_change
from backend.evaluation.stats import paired_smoke_statistics
from backend.models import (
    EvaluationGate, EvaluationManifest, EvaluationTrial,
    ImprovementCandidate,
)


class EvaluationConflict(Exception):
    pass


def sha(value: dict) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


async def freeze(
    session: AsyncSession, tenant_id: str, candidate_id: UUID,
) -> EvaluationManifest:
    async with session.begin():
        candidate = await session.scalar(
            select(ImprovementCandidate).where(
                ImprovementCandidate.id == candidate_id,
                ImprovementCandidate.tenant_id == tenant_id,
            ).with_for_update(read=True)
        )
        # Source invalidation updates the candidate before scanning its manifests.
        # A shared lock keeps that update ordered with registration.
        if candidate is None or candidate.state != "EVALUATION_READY":
            raise EvaluationConflict("candidate is not evaluation-ready")
        validate_change(candidate.kind, candidate.change_spec)
        payload = {"kind": candidate.kind, "change": candidate.change_spec}
        digest = sha(payload)
        fixed = fixture_digest()
        manifest_id = uuid5(
            NAMESPACE_URL,
            f"opsblaze/eval/{tenant_id}/{candidate_id}/{candidate.version}/{digest}/{fixed}",
        )
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif session.bind.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError("unsupported evaluation database")

        await session.execute(
            insert(EvaluationManifest)
            .values(
                id=manifest_id, tenant_id=tenant_id,
                candidate_id=candidate_id, candidate_version=candidate.version,
                candidate_digest=digest, incumbent_id="deterministic-v1",
                fixture_digest=fixed, oracle_version=ORACLE_VERSION,
                trial_budget=len(FIXTURES) * 2, state="FROZEN",
            )
            # Equal registrations can collide on both the natural key and the UUID.
            .on_conflict_do_nothing()
        )
        manifest = await session.get(EvaluationManifest, manifest_id)
        if manifest is None:
            raise EvaluationConflict("frozen manifest does not match this candidate version")
    return manifest


async def resume_smoke(
    session: AsyncSession,
    tenant_id: str,
    manifest_id: UUID,
    *,
    max_pairs: int | None = None,
) -> EvaluationManifest | None:
    """E5-06: execute or resume missing deterministic trial pairs without re-running completed ones."""
    if max_pairs is not None and not 1 <= max_pairs <= len(FIXTURES):
        raise EvaluationConflict("max_pairs must be between 1 and fixture count")
    async with session.begin():
        manifest = await session.scalar(
            select(EvaluationManifest).where(
                EvaluationManifest.id == manifest_id,
                EvaluationManifest.tenant_id == tenant_id,
            ).with_for_update()
        )
        if manifest is None:
            return None
        if manifest.state in {"SMOKE_PASS", "SMOKE_FAIL", "INVALID"}:
            return manifest
        if manifest.state not in {"FROZEN", "IN_PROGRESS"}:
            raise EvaluationConflict("manifest has an invalid state")
        candidate = await session.get(ImprovementCandidate, manifest.candidate_id)
        if (
            candidate is None or candidate.tenant_id != tenant_id
            or candidate.version != manifest.candidate_version
            or candidate.state != "EVALUATION_READY"
            or sha({"kind": candidate.kind, "change": candidate.change_spec})
                != manifest.candidate_digest
            or manifest.fixture_digest != fixture_digest()
            or manifest.oracle_version != ORACLE_VERSION
            or manifest.trial_budget != len(FIXTURES) * 2
        ):
            manifest.state = "INVALID"
            return manifest

        existing_trials = list((await session.scalars(
            select(EvaluationTrial).where(EvaluationTrial.manifest_id == manifest.id)
        )).all())
        by_key = {(t.pair_id, t.arm): t for t in existing_trials}
        config = candidate_config(candidate.kind, candidate.change_spec)

        pairs_executed = 0
        for case in FIXTURES:
            need_pair = any((case.id, arm) not in by_key for arm in ("INCUMBENT", "CANDIDATE"))
            if not need_pair:
                continue
            if max_pairs is not None and pairs_executed >= max_pairs:
                break
            expected = oracle_expected(case)
            for arm, arm_config in (("INCUMBENT", INCUMBENT), ("CANDIDATE", config)):
                if (case.id, arm) in by_key:
                    continue
                decision, units = evaluate_strategy(case, arm_config)
                correct = decision == expected
                unsafe = decision == "PROPOSE" and expected == "ABSTAIN"
                trial = EvaluationTrial(
                    id=uuid5(NAMESPACE_URL, f"{manifest.id}/{case.id}/{arm}"),
                    manifest_id=manifest.id, pair_id=case.id, arm=arm,
                    decision=decision, oracle_correct=correct,
                    unsafe=unsafe, probe_cost_units=units,
                )
                session.add(trial)
                by_key[(case.id, arm)] = trial
            pairs_executed += 1

        if len(by_key) < len(FIXTURES) * 2:
            manifest.state = "IN_PROGRESS"
            return manifest

        candidate_correct = sum(
            int(by_key[(case.id, "CANDIDATE")].oracle_correct) for case in FIXTURES
        )
        incumbent_correct = sum(
            int(by_key[(case.id, "INCUMBENT")].oracle_correct) for case in FIXTURES
        )
        candidate_unsafe = sum(
            int(by_key[(case.id, "CANDIDATE")].unsafe) for case in FIXTURES
        )
        candidate_cost = sum(
            by_key[(case.id, "CANDIDATE")].probe_cost_units for case in FIXTURES
        )
        incumbent_cost = sum(
            by_key[(case.id, "INCUMBENT")].probe_cost_units for case in FIXTURES
        )
        existing_rules = set((await session.scalars(
            select(EvaluationGate.rule_id).where(EvaluationGate.manifest_id == manifest.id)
        )).all())
        gates = (
            ("NO_UNSAFE", candidate_unsafe == 0,
             f"unsafe candidate proposals: {candidate_unsafe}"),
            ("NO_CORRECTNESS_REGRESSION", candidate_correct >= incumbent_correct,
             f"candidate correct {candidate_correct}/{len(FIXTURES)}, incumbent {incumbent_correct}"),
            ("PROBE_COST", candidate_cost <= incumbent_cost,
             f"candidate probes {candidate_cost}, incumbent {incumbent_cost}"),
        )
        for rule_id, passed, evidence in gates:
            if rule_id not in existing_rules:
                session.add(
                    EvaluationGate(
                        id=uuid5(NAMESPACE_URL, f"{manifest.id}/{rule_id}"),
                        manifest_id=manifest.id, rule_id=rule_id,
                        passed=passed, evidence=evidence,
                    )
                )
        manifest.state = "SMOKE_PASS" if all(g[1] for g in gates) else "SMOKE_FAIL"
    return manifest


async def run_smoke(
    session: AsyncSession, tenant_id: str, manifest_id: UUID,
) -> EvaluationManifest | None:
    return await resume_smoke(session, tenant_id, manifest_id, max_pairs=None)


async def supersede_manifest(
    session: AsyncSession,
    tenant_id: str,
    manifest_id: UUID,
    correction_reason: str,
) -> EvaluationManifest | None:
    """E5-06: retain prior incomplete/disputed manifest while issuing a superseding run."""
    clean_reason = correction_reason.strip()
    if not 8 <= len(clean_reason) <= 256:
        raise EvaluationConflict("correction reason must be 8–256 characters")
    async with session.begin():
        prior = await session.scalar(
            select(EvaluationManifest).where(
                EvaluationManifest.id == manifest_id,
                EvaluationManifest.tenant_id == tenant_id,
            ).with_for_update()
        )
        if prior is None:
            return None
        candidate = await session.scalar(
            select(ImprovementCandidate).where(
                ImprovementCandidate.id == prior.candidate_id,
                ImprovementCandidate.tenant_id == tenant_id,
            ).with_for_update()
        )
        if candidate is None or candidate.state != "EVALUATION_READY":
            raise EvaluationConflict("candidate is not evaluation-ready for supersession")
        validate_change(candidate.kind, candidate.change_spec)
        prior.state = "INVALID"
        candidate.version += 1
        digest = sha({"kind": candidate.kind, "change": candidate.change_spec})
        fixed = fixture_digest()
        replacement_id = uuid5(
            NAMESPACE_URL,
            f"opsblaze/eval/{tenant_id}/{candidate.id}/{candidate.version}/{digest}/{fixed}",
        )
        replacement = EvaluationManifest(
            id=replacement_id, tenant_id=tenant_id,
            candidate_id=candidate.id, candidate_version=candidate.version,
            candidate_digest=digest, incumbent_id="deterministic-v1",
            fixture_digest=fixed, oracle_version=ORACLE_VERSION,
            trial_budget=len(FIXTURES) * 2, state="FROZEN",
            supersedes_manifest_id=prior.id, correction_reason=clean_reason,
        )
        session.add(replacement)
    return replacement


async def read_trials_page(
    session: AsyncSession,
    tenant_id: str,
    manifest_id: UUID,
    *,
    cursor: UUID | None = None,
    limit: int = 25,
) -> dict | None:
    """E5-07: tenant-scoped bounded keyset pagination for evaluation trials."""
    if not 1 <= limit <= 50:
        raise ValueError("trial page limit must be 1–50")
    manifest = await session.scalar(
        select(EvaluationManifest.id).where(
            EvaluationManifest.id == manifest_id,
            EvaluationManifest.tenant_id == tenant_id,
        )
    )
    if manifest is None:
        return None
    stmt = select(EvaluationTrial).where(EvaluationTrial.manifest_id == manifest_id)
    if cursor is not None:
        stmt = stmt.where(EvaluationTrial.id > cursor)
    rows = list((await session.scalars(
        stmt.order_by(EvaluationTrial.id).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    return {
        "manifest_id": str(manifest_id),
        "records": [
            {
                "id": str(item.id),
                "pair_id": item.pair_id,
                "arm": item.arm,
                "decision": item.decision,
                "oracle_correct": item.oracle_correct,
                "unsafe": item.unsafe,
                "probe_cost_units": item.probe_cost_units,
            }
            for item in visible
        ],
        "next_cursor": str(visible[-1].id) if len(rows) > limit else None,
        "promotion_eligible": False,
    }


async def record_evaluation_review(
    session: AsyncSession,
    tenant_id: str,
    manifest_id: UUID,
    expected_state: str,
    decision: str,
    reviewer_id: str,
) -> EvaluationManifest | None:
    """E5-07: persist reviewer decision on a completed manifest without enabling promotion."""
    if decision not in {"ACKNOWLEDGED", "REJECTED_FOR_PROMOTION"}:
        raise EvaluationConflict("unsupported reviewer decision")
    async with session.begin():
        manifest = await session.scalar(
            select(EvaluationManifest).where(
                EvaluationManifest.id == manifest_id,
                EvaluationManifest.tenant_id == tenant_id,
            ).with_for_update()
        )
        if manifest is None:
            return None
        if manifest.state != expected_state:
            raise EvaluationConflict("manifest state changed; refetch before reviewing")
        if manifest.state not in {"SMOKE_PASS", "SMOKE_FAIL"}:
            raise EvaluationConflict("only completed evaluations may be reviewed")
        manifest.reviewer_decision = decision
        manifest.reviewer_id = reviewer_id[:48]
    return manifest


async def report(session: AsyncSession, tenant_id: str, manifest_id: UUID):
    manifest = await session.scalar(
        select(EvaluationManifest).where(
            EvaluationManifest.id == manifest_id,
            EvaluationManifest.tenant_id == tenant_id,
        )
    )
    if manifest is None:
        return None
    gates = (await session.scalars(
        select(EvaluationGate).where(EvaluationGate.manifest_id == manifest_id)
        .order_by(EvaluationGate.rule_id)
    )).all()
    trials = (await session.scalars(
        select(EvaluationTrial).where(EvaluationTrial.manifest_id == manifest_id)
        .order_by(EvaluationTrial.pair_id, EvaluationTrial.arm)
    )).all()
    statistics = paired_smoke_statistics(
        trials, [fixture.id for fixture in FIXTURES]
    )
    return {
        "id": str(manifest.id),
        "integrity": statistics["integrity"],
        "statistics": statistics,
        "candidate_id": str(manifest.candidate_id),
        "candidate_version": manifest.candidate_version,
        "state": manifest.state,
        "supersedes_manifest_id": str(manifest.supersedes_manifest_id) if manifest.supersedes_manifest_id else None,
        "correction_reason": manifest.correction_reason,
        "reviewer_decision": manifest.reviewer_decision,
        "fixture_digest": manifest.fixture_digest,
        "oracle_version": manifest.oracle_version,
        "promotion_eligible": False,
        "gates": [
            {"rule_id": item.rule_id, "passed": item.passed, "evidence": item.evidence}
            for item in gates
        ],
        "trials": [
            {"pair_id": item.pair_id, "arm": item.arm,
             "decision": item.decision, "oracle_correct": item.oracle_correct,
             "unsafe": item.unsafe, "probe_cost_units": item.probe_cost_units}
            for item in trials
        ],
    }
