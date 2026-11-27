
"""S1 deterministic paired replay. No production credentials or model endpoints."""

from dataclasses import dataclass, field
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ReplayArm, ReplayExperiment


class FutureEvidenceDenied(Exception):
    pass


class ProbeBudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Fixture:
    id: str
    initially_healthy: bool
    false_success_ack: bool


FIXTURES = {
    "recoverable-v1": Fixture("recoverable-v1", False, False),
    "false-ack-v1": Fixture("false-ack-v1", False, True),
    "already-healthy-v1": Fixture("already-healthy-v1", True, False),
}
EVIDENCE_TIMES = {"health": 0, "version": 2}
PROBE_BUDGET = 2


WORKLOAD_VERSION = "workload-v1"
EVIDENCE_POLICY_VERSION = "causal-v1"
RUNBOOK_POLICY_VERSION = "runbook-static-v1"
ADAPTIVE_POLICY_VERSION = "adaptive-gated-v1"
ARM_KINDS = ("RUNBOOK", "ADAPTIVE_BASELINE")


def fixture_digest(fixture: Fixture, seed: int) -> str:
    manifest = {
        "fixture": fixture.id, "seed": seed,
        "initially_healthy": fixture.initially_healthy,
        "false_ack": fixture.false_success_ack,
        "evidence_times": EVIDENCE_TIMES,
        "budget": PROBE_BUDGET,
        "oracle_version": "postcondition-v1",
        "workload_version": WORKLOAD_VERSION,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "runbook_policy_version": RUNBOOK_POLICY_VERSION,
        "adaptive_policy_version": ADAPTIVE_POLICY_VERSION,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Twin:
    fixture: Fixture
    seed: int
    budget: int = PROBE_BUDGET
    virtual_time: int = 0
    calls: int = 0
    trace: list[dict] = field(default_factory=list)
    healthy: bool = field(init=False)
    version: int = field(init=False)

    def __post_init__(self) -> None:
        self.healthy = self.fixture.initially_healthy
        self.version = 1 + (self.seed % 3)

    def advance(self, instant: int) -> None:
        if instant < self.virtual_time:
            raise ValueError("virtual time must be monotonic")
        self.virtual_time = instant

    def probe(self, kind: str):
        if kind not in EVIDENCE_TIMES:
            raise ValueError("probe is not allowlisted")
        if self.virtual_time < EVIDENCE_TIMES[kind]:
            self.trace.append({"kind": kind, "time": self.virtual_time, "outcome": "FUTURE_DENIED"})
            raise FutureEvidenceDenied(kind)
        if self.calls >= self.budget:
            self.trace.append({"kind": kind, "time": self.virtual_time, "outcome": "BUDGET_DENIED"})
            raise ProbeBudgetExceeded(kind)
        self.calls += 1
        value = self.healthy if kind == "health" else self.version
        self.trace.append({"kind": kind, "time": self.virtual_time, "outcome": "OBSERVED"})
        return value

    def apply_simulated_recovery(self, expected_version: int) -> str:
        if self.version != expected_version:
            return "VERSION_REJECTED"
        if not self.fixture.false_success_ack:
            self.healthy = True
            self.version += 1
        return "SUCCESS_ACK"

    def independent_oracle(self) -> bool:
        # Never accept the effect adapter's success ACK as recovery evidence.
        return self.healthy and not self.fixture.initially_healthy


def run_arm(fixture: Fixture, seed: int, kind: str) -> Twin:
    twin = Twin(fixture, seed)
    if kind not in ARM_KINDS:
        raise ValueError("unsupported replay arm")
    # Each arm gets its own mutable twin and independently gated observations.
    healthy = twin.probe("health")
    if kind == "ADAPTIVE_BASELINE" and healthy:
        twin.trace.append({"kind": "decision", "time": twin.virtual_time, "outcome": "HEALTHY_ABSTAIN"})
        return twin
    twin.advance(2)
    version = twin.probe("version")
    if not healthy:
        result = twin.apply_simulated_recovery(version)
        twin.trace.append({"kind": "effect", "time": twin.virtual_time, "outcome": result})
    else:
        twin.trace.append({"kind": "decision", "time": twin.virtual_time, "outcome": "RUNBOOK_NOOP"})
    return twin


def _build_arm(experiment_id: UUID, fixture: Fixture, seed: int, kind: str) -> ReplayArm:
    twin = run_arm(fixture, seed, kind)
    passed = twin.independent_oracle()
    outcome = "VERIFIED" if passed else (
        "ABSTAINED" if fixture.initially_healthy else "FAILED"
    )
    return ReplayArm(
        id=uuid4(), experiment_id=experiment_id, kind=kind,
        namespace=f"{experiment_id}/{kind.lower()}",
        virtual_time=twin.virtual_time, outcome=outcome, probe_cost=twin.calls,
        oracle_passed=passed, trace=twin.trace,
    )


def _arm_result(arm: ReplayArm) -> dict:
    return {
        "kind": arm.kind, "namespace": arm.namespace, "virtual_time": arm.virtual_time,
        "outcome": arm.outcome, "probe_cost": arm.probe_cost,
        "oracle_passed": arm.oracle_passed, "trace": arm.trace,
    }


async def start_pair(
    session: AsyncSession, tenant_id: str, fixture_id: str, seed: int,
    *, initial_arms: int = 1,
) -> ReplayExperiment:
    """Create a durable paired experiment halted mid-flight for resumable replay."""
    if fixture_id not in FIXTURES or not 0 <= seed <= 1_000_000:
        raise ValueError("unsupported replay fixture or seed")
    if initial_arms not in (0, 1):
        raise ValueError("initial_arms must be 0 or 1")
    fixture = FIXTURES[fixture_id]
    experiment_id = uuid4()
    with_seed = fixture_digest(fixture, seed)
    async with session.begin():
        experiment = ReplayExperiment(
            id=experiment_id, tenant_id=tenant_id,
            fixture_id=fixture_id, fixture_digest=with_seed,
            seed=seed, budget=PROBE_BUDGET,
            status="RUNNING" if initial_arms == 1 else "PENDING",
            workload_version=WORKLOAD_VERSION,
            evidence_policy_version=EVIDENCE_POLICY_VERSION,
        )
        session.add(experiment)
        if initial_arms == 1:
            session.add(_build_arm(experiment_id, fixture, seed, "RUNBOOK"))
    return experiment


async def resume_pair(
    session: AsyncSession, tenant_id: str, experiment_id: UUID,
    *, inject_leak: bool = False,
) -> ReplayExperiment | None:
    """Resume missing arms deterministically; invalidate leaking or incomplete pairs."""
    async with session.begin():
        experiment = await session.scalar(
            select(ReplayExperiment).where(
                ReplayExperiment.id == experiment_id,
                ReplayExperiment.tenant_id == tenant_id,
            ).with_for_update()
        )
        if experiment is None:
            return None
        if experiment.status in {"COMPLETE", "INVALIDATED"}:
            return experiment
        fixture = FIXTURES.get(experiment.fixture_id)
        if (
            fixture is None
            or not 0 <= experiment.seed <= 1_000_000
            or experiment.budget != PROBE_BUDGET
            or experiment.workload_version != WORKLOAD_VERSION
            or experiment.evidence_policy_version != EVIDENCE_POLICY_VERSION
            or experiment.fixture_digest != fixture_digest(fixture, experiment.seed)
        ):
            experiment.status = "INVALIDATED"
            experiment.invalidation_reason = "FIXTURE_DIGEST_MISMATCH"
            return experiment
        existing_arms = {
            arm.kind: arm for arm in (await session.scalars(
                select(ReplayArm).where(ReplayArm.experiment_id == experiment.id)
            )).all()
        }
        expected_arms = {
            kind: _build_arm(experiment.id, fixture, experiment.seed, kind) for kind in ARM_KINDS
        }
        if any(
            kind not in expected_arms
            or json.dumps(_arm_result(stored), sort_keys=True)
            != json.dumps(_arm_result(expected_arms[kind]), sort_keys=True)
            for kind, stored in existing_arms.items()
        ):
            experiment.status = "INVALIDATED"
            experiment.invalidation_reason = "STORED_ARM_MISMATCH"
            return experiment
        if inject_leak:
            leaking = Twin(fixture, experiment.seed)
            try:
                leaking.probe("version")
            except FutureEvidenceDenied:
                pass
            experiment.status = "INVALIDATED"
            experiment.invalidation_reason = "FUTURE_EVIDENCE_OR_CONTAMINATION"
            if "ADAPTIVE_BASELINE" not in existing_arms:
                session.add(
                    ReplayArm(
                        id=uuid4(), experiment_id=experiment.id, kind="ADAPTIVE_BASELINE",
                        namespace=f"{experiment.id}/adaptive_baseline",
                        virtual_time=leaking.virtual_time, outcome="INVALIDATED",
                        probe_cost=leaking.calls, oracle_passed=False,
                        trace=leaking.trace,
                    )
                )
            return experiment

        session.add_all(arm for kind, arm in expected_arms.items() if kind not in existing_arms)
        experiment.status = "COMPLETE"
        return experiment


async def list_pairs_page(
    session: AsyncSession, tenant_id: str,
    *, cursor: UUID | None = None, limit: int = 50,
) -> tuple[list[ReplayExperiment], str | None]:
    if not 1 <= limit <= 50:
        raise ValueError("experiment page limit must be 1–50")
    stmt = select(ReplayExperiment).where(ReplayExperiment.tenant_id == tenant_id)
    if cursor is not None:
        stmt = stmt.where(ReplayExperiment.id > cursor)
    rows = list((await session.scalars(
        stmt.order_by(ReplayExperiment.id).limit(limit + 1)
    )).all())
    visible = rows[:limit]
    return visible, str(visible[-1].id) if len(rows) > limit else None


async def run_paired_trials(
    session: AsyncSession, tenant_id: str, fixture_id: str, seeds: list[int],
) -> dict:
    """S1-07: repeated paired comparison between RUNBOOK and ADAPTIVE_BASELINE arms."""
    if fixture_id not in FIXTURES:
        raise ValueError("unsupported replay fixture")
    if not seeds or len(seeds) > 20 or any(not 0 <= s <= 1_000_000 for s in seeds):
        raise ValueError("paired trials require 1–20 valid seeds")
    fixture = FIXTURES[fixture_id]
    runs: list[UUID] = []
    runbook_verified = adaptive_verified = 0
    runbook_cost = adaptive_cost = 0
    async with session.begin():
        for seed in seeds:
            experiment_id = uuid4()
            runs.append(experiment_id)
            session.add(
                ReplayExperiment(
                    id=experiment_id, tenant_id=tenant_id,
                    fixture_id=fixture_id, fixture_digest=fixture_digest(fixture, seed),
                    seed=seed, budget=PROBE_BUDGET, status="COMPLETE",
                    workload_version=WORKLOAD_VERSION,
                    evidence_policy_version=EVIDENCE_POLICY_VERSION,
                )
            )
            for kind in ARM_KINDS:
                arm = _build_arm(experiment_id, fixture, seed, kind)
                session.add(arm)
                if kind == "RUNBOOK":
                    runbook_verified += int(arm.oracle_passed)
                    runbook_cost += arm.probe_cost
                else:
                    adaptive_verified += int(arm.oracle_passed)
                    adaptive_cost += arm.probe_cost
    return {
        "fixture_id": fixture_id,
        "trials": len(seeds),
        "experiment_ids": [str(eid) for eid in runs],
        "runbook_verified": runbook_verified,
        "adaptive_verified": adaptive_verified,
        "runbook_probe_cost": runbook_cost,
        "adaptive_probe_cost": adaptive_cost,
        "adaptive_probe_saved": runbook_cost - adaptive_cost,
        "measured_ai_comparison": False,
    }


async def create_pair(
    session: AsyncSession, tenant_id: str, fixture_id: str, seed: int,
) -> ReplayExperiment:
    if fixture_id not in FIXTURES or not 0 <= seed <= 1_000_000:
        raise ValueError("unsupported replay fixture or seed")
    fixture = FIXTURES[fixture_id]
    experiment_id = uuid4()
    with_seed = fixture_digest(fixture, seed)
    async with session.begin():
        experiment = ReplayExperiment(
            id=experiment_id, tenant_id=tenant_id,
            fixture_id=fixture_id, fixture_digest=with_seed,
            seed=seed, budget=PROBE_BUDGET, status="COMPLETE",
            workload_version=WORKLOAD_VERSION,
            evidence_policy_version=EVIDENCE_POLICY_VERSION,
        )
        session.add(experiment)
        session.add_all(_build_arm(experiment_id, fixture, seed, kind) for kind in ARM_KINDS)
    return experiment


async def pair_report(session: AsyncSession, tenant_id: str, experiment_id: UUID):
    # A single statement observes the pair and its arms in the same snapshot.
    rows = (await session.execute(
        select(ReplayExperiment, ReplayArm)
        .outerjoin(ReplayArm, ReplayArm.experiment_id == ReplayExperiment.id)
        .where(
            ReplayExperiment.id == experiment_id,
            ReplayExperiment.tenant_id == tenant_id,
        ).order_by(ReplayArm.kind)
    )).all()
    if not rows:
        return None
    experiment = rows[0][0]
    return {
        "id": str(experiment.id), "fixture_id": experiment.fixture_id,
        "fixture_digest": experiment.fixture_digest, "seed": experiment.seed,
        "status": experiment.status, "budget": experiment.budget,
        "workload_version": experiment.workload_version,
        "evidence_policy_version": experiment.evidence_policy_version,
        "invalidation_reason": experiment.invalidation_reason,
        "measured_ai_comparison": False,
        "arms": [_arm_result(arm) for _, arm in rows if arm is not None],
    }
