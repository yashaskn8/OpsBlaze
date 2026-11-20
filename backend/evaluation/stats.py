"""Conservative paired smoke statistics, not production performance claims."""

from math import log, sqrt


def paired_smoke_statistics(trials: list, expected_pair_ids: list[str]) -> dict:
    expected = set(expected_pair_ids)
    seen = {}
    for trial in trials:
        key = (trial.pair_id, trial.arm)
        if key in seen:
            return {"integrity": "INVALID", "reason": "duplicate trial arm"}
        seen[key] = trial

    required = {
        (pair_id, arm)
        for pair_id in expected
        for arm in ("INCUMBENT", "CANDIDATE")
    }
    if set(seen) != required:
        return {
            "integrity": "INCOMPLETE",
            "expected_arms": len(required),
            "observed_arms": len(seen),
            "verified_recovery_per_cost": None,
        }

    diffs = []
    incumbent_cost = candidate_cost = 0
    candidate_unsafe = incumbent_unsafe = 0
    for pair_id in sorted(expected):
        incumbent = seen[(pair_id, "INCUMBENT")]
        candidate = seen[(pair_id, "CANDIDATE")]
        diffs.append(int(candidate.oracle_correct) - int(incumbent.oracle_correct))
        incumbent_cost += incumbent.probe_cost_units
        candidate_cost += candidate.probe_cost_units
        incumbent_unsafe += int(incumbent.unsafe)
        candidate_unsafe += int(candidate.unsafe)

    n = len(diffs)
    if n == 0:
        return {"integrity": "INCOMPLETE", "reason": "empty fixture set"}
    mean = sum(diffs) / n
    # Hoeffding for paired {-1,0,+1} differences, with range width 2:
    # P(abs(mean - expectation) > epsilon) <= 2 exp(-n epsilon^2/2).
    # Very small fixture sets produce deliberately wide intervals.
    margin = sqrt(2 * log(40) / n)
    interval = [
        round(max(-1.0, mean - margin), 4),
        round(min(1.0, mean + margin), 4),
    ]
    return {
        "integrity": "COMPLETE",
        "pairs": n,
        "candidate_wins": sum(value == 1 for value in diffs),
        "incumbent_wins": sum(value == -1 for value in diffs),
        "ties": sum(value == 0 for value in diffs),
        "paired_correctness_delta": round(mean, 4),
        "conservative_95pct_interval": interval,
        "interval_method": "Hoeffding_bound_on_paired_binary_correctness",
        "candidate_probe_cost_units": candidate_cost,
        "incumbent_probe_cost_units": incumbent_cost,
        "candidate_unsafe": candidate_unsafe,
        "incumbent_unsafe": incumbent_unsafe,
        # These public smoke fixtures have no independent effect/cost data.
        "verified_recovery_per_cost": None,
        "held_out": False,
        "eligible_for_promotion": False,
    }
