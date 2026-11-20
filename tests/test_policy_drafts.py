
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import (
    EvaluationManifest, FailureCluster, ImprovementCandidate, PolicyBundle,
    PolicyRollout,
)
from backend.policy.registry import (
    PolicyConflict, create_rollout, dry_run_assignment, register_bundle,
    stop_rollout,
)
from backend.evaluation.fixtures import fixture_digest, ORACLE_VERSION
from backend.evaluation.smoke import sha


@pytest.fixture
async def safe_evaluation(sessions_fixture):
    cluster_id = uuid4()
    candidate_id = uuid4()
    eval_id = uuid4()
    change = {"max_context_tokens": 512}
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(FailureCluster(
                id=cluster_id, tenant_id="tenant", signature="source",
                version=1, case_count=1, quarantined=False,
            ))
            session.add(ImprovementCandidate(
                id=candidate_id, tenant_id="tenant",
                source_cluster_id=cluster_id, kind="CONTEXT_LIMIT",
                base_policy_id="baseline-v1", change_spec=change,
                hypothesis="maintain sufficient context", risk="LOW",
                evaluation_manifest_id="fixed-smoke-v1",
                state="EVALUATION_READY", version=2,
                created_by="local_operator",
            ))
            session.add(EvaluationManifest(
                id=eval_id, tenant_id="tenant", candidate_id=candidate_id,
                candidate_version=2,
                candidate_digest=sha({"kind": "CONTEXT_LIMIT", "change": change}),
                incumbent_id="baseline-v1", fixture_digest=fixture_digest(),
                oracle_version=ORACLE_VERSION,
                trial_budget=12, state="SMOKE_PASS",
            ))
    return sessions_fixture, candidate_id, eval_id


@pytest.mark.asyncio
async def test_policy_digest_is_frozen_and_does_not_enable_exposure(safe_evaluation):
    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        first = await register_bundle(session, "tenant", candidate_id, eval_id)
        assert first.state == "DRAFT"
    async with sessions() as session:
        second = await register_bundle(session, "tenant", candidate_id, eval_id)
        assert second.id == first.id
        assert second.content["tool_catalog"] == "simulator-readonly-v1"
    async with sessions() as session:
        rollout = await create_rollout(session, "tenant", first.id, 5)
        assert rollout.state == "DRAFT"
        first_assignment = dry_run_assignment(rollout, "tenant", uuid4())
        assert type(first_assignment) is bool
        assert dry_run_assignment(rollout, "another-tenant", uuid4()) is False
    async with sessions() as session:
        paused = await stop_rollout(session, "tenant", rollout.id, 1, "PAUSED")
        assert paused.state == "PAUSED" and paused.version == 2
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await stop_rollout(session, "tenant", rollout.id, 1, "ROLLED_BACK")
    async with sessions() as session:
        rolled = await stop_rollout(session, "tenant", rollout.id, 2, "ROLLED_BACK")
        assert rolled.state == "ROLLED_BACK" and rolled.version == 3


@pytest.mark.asyncio
async def test_rollout_scope_and_cohort_are_restricted(safe_evaluation):
    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        bundle = await register_bundle(session, "tenant", candidate_id, eval_id)
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await create_rollout(session, "tenant", bundle.id, 100)
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await create_rollout(session, "other", bundle.id, 1)
