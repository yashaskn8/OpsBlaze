
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
            )
        )
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
        manifest = await session.get(EvaluationManifest, manifest_id)
        if manifest is not None:
            return manifest
        manifest = EvaluationManifest(
            id=manifest_id, tenant_id=tenant_id,
            candidate_id=candidate_id, candidate_version=candidate.version,
            candidate_digest=digest, incumbent_id="deterministic-v1",
            fixture_digest=fixed, oracle_version=ORACLE_VERSION,
            trial_budget=len(FIXTURES) * 2, state="FROZEN",
        )
        session.add(manifest)
    return manifest


async def run_smoke(
    session: AsyncSession, tenant_id: str, manifest_id: UUID,
) -> EvaluationManifest | None:
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
        if manifest.state != "FROZEN":
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

        config = candidate_config(candidate.kind, candidate.change_spec)
        candidate_correct = incumbent_correct = 0
        candidate_unsafe = 0
        candidate_cost = incumbent_cost = 0
        for case in FIXTURES:
            expected = oracle_expected(case)
            for arm, arm_config in (("INCUMBENT", INCUMBENT), ("CANDIDATE", config)):
                decision, units = evaluate_strategy(case, arm_config)
                correct = decision == expected
                unsafe = decision == "PROPOSE" and expected == "ABSTAIN"
                if arm == "CANDIDATE":
                    candidate_correct += int(correct)
                    candidate_unsafe += int(unsafe)
                    candidate_cost += units
                else:
                    incumbent_correct += int(correct)
                    incumbent_cost += units
                session.add(
                    EvaluationTrial(
                        id=uuid5(NAMESPACE_URL, f"{manifest.id}/{case.id}/{arm}"),
                        manifest_id=manifest.id, pair_id=case.id, arm=arm,
                        decision=decision, oracle_correct=correct,
                        unsafe=unsafe, probe_cost_units=units,
                    )
                )

        gates = (
            ("NO_UNSAFE", candidate_unsafe == 0,
             f"unsafe candidate proposals: {candidate_unsafe}"),
            ("NO_CORRECTNESS_REGRESSION", candidate_correct >= incumbent_correct,
             f"candidate correct {candidate_correct}/{len(FIXTURES)}, incumbent {incumbent_correct}"),
            ("PROBE_COST", candidate_cost <= incumbent_cost,
             f"candidate probes {candidate_cost}, incumbent {incumbent_cost}"),
        )
        for rule_id, passed, evidence in gates:
            session.add(
                EvaluationGate(
                    id=uuid5(NAMESPACE_URL, f"{manifest.id}/{rule_id}"),
                    manifest_id=manifest.id, rule_id=rule_id,
                    passed=passed, evidence=evidence,
                )
            )
        manifest.state = "SMOKE_PASS" if all(g[1] for g in gates) else "SMOKE_FAIL"
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
    return {
        "id": str(manifest.id),
        "candidate_id": str(manifest.candidate_id),
        "candidate_version": manifest.candidate_version,
        "state": manifest.state,
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
