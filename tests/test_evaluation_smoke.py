
from uuid import uuid4

import pytest
from sqlalchemy import delete

from backend.evaluation.fixtures import (
    FIXTURES, candidate_config, evaluate_strategy, oracle_expected,
)
from backend.evaluation.smoke import freeze, report, run_smoke
from backend.evaluation.stats import paired_smoke_statistics
from backend.models import (
    EvaluationTrial, FailureCluster, ImprovementCandidate,
)


@pytest.fixture
async def candidate_lab(sessions_fixture):
    cluster_id = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                FailureCluster(
                    id=cluster_id, tenant_id="t", signature="EFFECT_FAILED:fixture",
                    version=1, case_count=1, quarantined=False,
                )
            )
            good_id = uuid4()
            bad_id = uuid4()
            for candidate_id, spec in (
                (good_id, {"max_context_tokens": 512}),
                (bad_id, {"max_context_tokens": 64}),
            ):
                session.add(
                    ImprovementCandidate(
                        id=candidate_id, tenant_id="t", source_cluster_id=cluster_id,
                        kind="CONTEXT_LIMIT", base_policy_id="deterministic-v1",
                        change_spec=spec, hypothesis="bounded probe context",
                        risk="LOW", evaluation_manifest_id="fixed-smoke-v1",
                        state="EVALUATION_READY", version=2,
                        created_by="local_operator",
                    )
                )
    return sessions_fixture, good_id, bad_id


@pytest.mark.asyncio
async def test_frozen_manifest_idempotent_and_good_smoke(candidate_lab):
    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        first = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        second = await freeze(session, "t", candidate_id)
        assert second.id == first.id
    async with sessions() as session:
        completed = await run_smoke(session, "t", first.id)
        assert completed.state == "SMOKE_PASS"
    async with sessions() as session:
        again = await run_smoke(session, "t", first.id)
        assert again.state == "SMOKE_PASS"
        data = await report(session, "t", first.id)
        assert len(data["trials"]) == 2 * len(FIXTURES)
        assert all(gate["passed"] for gate in data["gates"])
        assert data["integrity"] == "COMPLETE"
        assert data["statistics"]["pairs"] == len(FIXTURES)
        assert data["statistics"]["candidate_wins"] == 0
        assert data["statistics"]["verified_recovery_per_cost"] is None
        assert data["statistics"]["conservative_95pct_interval"] == [-1.0, 1.0]
        assert data["promotion_eligible"] is False
        assert await report(session, "another-tenant", first.id) is None


@pytest.mark.asyncio
async def test_regression_is_detected_and_does_not_promote(candidate_lab):
    sessions, _, candidate_id = candidate_lab
    async with sessions() as session:
        manifest = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        result = await run_smoke(session, "t", manifest.id)
        assert result.state == "SMOKE_FAIL"
    async with sessions() as session:
        data = await report(session, "t", manifest.id)
        assert any(not gate["passed"] for gate in data["gates"])


def test_oracle_negative_controls_never_propose_unsafe_action():
    for case in FIXTURES:
        decision, _ = evaluate_strategy(
            case, candidate_config("CONTEXT_LIMIT", {"max_context_tokens": 512})
        )
        if case.id in {"stale-target", "injected-lesson", "disputed-outcome"}:
            assert oracle_expected(case) == "ABSTAIN"
            assert decision == "ABSTAIN"


@pytest.mark.asyncio
async def test_missing_paired_arm_invalidates_report_integrity(candidate_lab):
    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        manifest = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        await run_smoke(session, "t", manifest.id)
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                delete(EvaluationTrial).where(
                    EvaluationTrial.manifest_id == manifest.id,
                    EvaluationTrial.pair_id == FIXTURES[0].id,
                    EvaluationTrial.arm == "CANDIDATE",
                )
            )
    async with sessions() as session:
        data = await report(session, "t", manifest.id)
        assert data["integrity"] == "INCOMPLETE"
        assert data["statistics"]["verified_recovery_per_cost"] is None
        assert data["promotion_eligible"] is False


def test_paired_statistics_rejects_duplicate_or_unpaired_trials():
    from types import SimpleNamespace

    trial = SimpleNamespace(
        pair_id="one", arm="INCUMBENT", oracle_correct=True,
        unsafe=False, probe_cost_units=1,
    )
    assert paired_smoke_statistics([trial], ["one"])["integrity"] == "INCOMPLETE"
    assert paired_smoke_statistics([trial, trial], ["one"])["integrity"] == "INVALID"
