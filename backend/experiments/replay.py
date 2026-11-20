
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


def fixture_digest(fixture: Fixture, seed: int) -> str:
    manifest = {
        "fixture": fixture.id, "seed": seed,
        "initially_healthy": fixture.initially_healthy,
        "false_ack": fixture.false_success_ack,
        "evidence_times": EVIDENCE_TIMES,
        "budget": PROBE_BUDGET,
        "oracle_version": "postcondition-v1",
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
    if kind not in {"RUNBOOK", "ADAPTIVE_BASELINE"}:
        raise ValueError("unsupported replay arm")
    # Each arm gets its own mutable twin and independently gated observations.
    healthy = twin.probe("health")
    if not healthy:
        twin.advance(2)
        version = twin.probe("version")
        result = twin.apply_simulated_recovery(version)
        twin.trace.append({"kind": "effect", "time": twin.virtual_time, "outcome": result})
    return twin


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
        )
        session.add(experiment)
        for kind in ("RUNBOOK", "ADAPTIVE_BASELINE"):
            twin = run_arm(fixture, seed, kind)
            passed = twin.independent_oracle()
            outcome = "VERIFIED" if passed else (
                "ABSTAINED" if fixture.initially_healthy else "FAILED"
            )
            session.add(
                ReplayArm(
                    id=uuid4(), experiment_id=experiment_id, kind=kind,
                    namespace=f"{experiment_id}/{kind.lower()}",
                    virtual_time=twin.virtual_time, outcome=outcome,
                    probe_cost=twin.calls, oracle_passed=passed,
                    trace=twin.trace,
                )
            )
    return experiment


async def pair_report(session: AsyncSession, tenant_id: str, experiment_id: UUID):
    experiment = await session.scalar(
        select(ReplayExperiment).where(
            ReplayExperiment.id == experiment_id,
            ReplayExperiment.tenant_id == tenant_id,
        )
    )
    if experiment is None:
        return None
    arms = (await session.scalars(
        select(ReplayArm).where(ReplayArm.experiment_id == experiment.id)
        .order_by(ReplayArm.kind)
    )).all()
    return {
        "id": str(experiment.id), "fixture_id": experiment.fixture_id,
        "fixture_digest": experiment.fixture_digest, "seed": experiment.seed,
        "status": experiment.status, "budget": experiment.budget,
        "measured_ai_comparison": False,
        "arms": [
            {"kind": item.kind, "namespace": item.namespace,
             "virtual_time": item.virtual_time, "outcome": item.outcome,
             "probe_cost": item.probe_cost, "oracle_passed": item.oracle_passed,
             "trace": item.trace}
            for item in arms
        ],
    }
