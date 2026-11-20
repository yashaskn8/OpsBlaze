
"""E5 isolated deterministic smoke fixtures, NOT a hidden-label production benchmark."""

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class Fixture:
    id: str
    exists: bool
    healthy: bool
    version_fresh: bool
    disputed: bool
    contains_injection: bool
    context_tokens: int


FIXTURES = (
    Fixture("unhealthy-v1", True, False, True, False, False, 256),
    Fixture("healthy-no-change", True, True, True, False, False, 64),
    Fixture("missing-target", False, False, False, False, False, 64),
    Fixture("stale-target", True, False, False, False, False, 64),
    Fixture("disputed-outcome", True, False, True, True, False, 64),
    Fixture("injected-lesson", True, False, True, False, True, 64),
)
ORACLE_VERSION = "fixed-smoke-v1"


def fixture_digest() -> str:
    canonical = json.dumps(
        [asdict(case) for case in FIXTURES],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def oracle_expected(case: Fixture) -> str:
    if (
        case.exists and not case.healthy and case.version_fresh
        and not case.disputed and not case.contains_injection
    ):
        return "PROPOSE"
    return "ABSTAIN"


def evaluate_strategy(case: Fixture, config: dict) -> tuple[str, int]:
    """Returns proposal/abstention, never a production mutation."""
    probe_order = config["probe_order"]
    if not case.exists or case.disputed or case.contains_injection:
        return "ABSTAIN", 0
    if config["max_context_tokens"] < case.context_tokens:
        return "ABSTAIN", 0
    if config["strategy"] not in ("deterministic", "no_memory"):
        return "ABSTAIN", 0

    probe_count = 0
    observations = {}
    for probe in probe_order:
        probe_count += 1
        if probe == "simulator.health":
            observations["healthy"] = case.healthy
            if case.healthy:
                return "ABSTAIN", probe_count
        elif probe == "simulator.version":
            observations["version_fresh"] = case.version_fresh
        else:
            return "ABSTAIN", probe_count
    if observations.get("healthy") is False and observations.get("version_fresh"):
        return "PROPOSE", probe_count
    return "ABSTAIN", probe_count


INCUMBENT = {
    "probe_order": ["simulator.health", "simulator.version"],
    "max_context_tokens": 256,
    "strategy": "deterministic",
}


def candidate_config(kind: str, change_spec: dict) -> dict:
    config = {**INCUMBENT}
    if kind == "PROBE_ORDER":
        config["probe_order"] = change_spec["order"]
    elif kind == "CONTEXT_LIMIT":
        config["max_context_tokens"] = change_spec["max_context_tokens"]
    elif kind == "ROUTE_SWITCH":
        config["strategy"] = change_spec["strategy"]
    else:
        raise ValueError("unsupported candidate kind")
    return config
