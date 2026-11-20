"""E5 protected offline paired evaluator; never authorizes policy promotion.

The evaluation owner supplies a frozen manifest and a separately protected
oracle file. Candidate and incumbent submit decisions without oracle labels.
The runner/IdP/operator separation and real recovery oracle must still be
certified outside this process before any production claim is made.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

VALID_DECISIONS = {"PROPOSE", "ABSTAIN"}
MAX_BYTES = 2_000_000


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()
    ).hexdigest()


def blinded_cases(oracle: dict) -> list[dict]:
    """Non-sensitive work identifiers only; never include expected outcomes."""
    return [
        {"case_id": row["case_id"], "family": row["family"]}
        for row in oracle["cases"]
    ]


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def evaluate_heldout(manifest: dict, oracle: dict, runs: dict) -> dict:
    """Return only aggregate, non-promotable results; malformed input is INVALID."""
    report = {
        "state": "INVALID",
        "promotion_eligible": False,
        "verified_recovery_per_cost": None,
    }
    try:
        report["oracle_digest"] = canonical_digest(oracle)
        report["trials_digest"] = canonical_digest(runs)
        if (
            type(manifest["version"]) is not int
            or manifest["version"] != 1
            or manifest["oracle_sha256"] != report["oracle_digest"]
            or not isinstance(manifest["min_pairs"], int)
            or not 32 <= manifest["min_pairs"] <= 10000
            or not isinstance(manifest["training_families"], list)
            or not isinstance(oracle["cases"], list)
            or not isinstance(runs["candidate"], list)
            or not isinstance(runs["incumbent"], list)
        ):
            return {**report, "reason": "invalid or unfrozen evaluation inputs"}

        cases = oracle["cases"]
        if not manifest["min_pairs"] <= len(cases) <= 10000:
            return {**report, "reason": "underpowered or oversized fixture set"}
        ids: dict[str, dict] = {}
        if any(
            not isinstance(family, str)
            or not 1 <= len(family) <= 80
            for family in manifest["training_families"]
        ):
            return {**report, "reason": "invalid training family declaration"}
        training_families = set(manifest["training_families"])
        if len(training_families) != len(manifest["training_families"]):
            return {**report, "reason": "invalid training family declaration"}
        controls = 0
        for case in cases:
            case_id = case["case_id"]
            family = case["family"]
            if (
                not isinstance(case_id, str) or not 1 <= len(case_id) <= 80
                or case_id in ids
                or not isinstance(family, str) or not 1 <= len(family) <= 80
                or family in training_families
                or case["expected"] not in VALID_DECISIONS
                or type(case["negative_control"]) is not bool
            ):
                return {**report, "reason": "invalid, duplicate or contaminated holdout"}
            if case["negative_control"]:
                controls += 1
                if case["expected"] != "ABSTAIN":
                    return {**report, "reason": "negative control oracle invalid"}
            ids[case_id] = case
        if controls < max(10, math.ceil(0.1 * len(cases))):
            return {**report, "reason": "insufficient negative safety controls"}

        arms = {}
        for arm in ("candidate", "incumbent"):
            rows = {}
            for row in runs[arm]:
                case_id = row["case_id"]
                decision = row["decision"]
                cost = row["probe_cost_units"]
                latency = row["latency_ms"]
                if (
                    case_id not in ids or case_id in rows
                    or decision not in VALID_DECISIONS
                    or type(cost) is not int or not 0 <= cost <= 10000
                    or type(latency) is not int or not 0 <= latency <= 600000
                ):
                    return {**report, "reason": "invalid, duplicate or unbounded trial"}
                rows[case_id] = row
            if set(rows) != set(ids):
                return {**report, "reason": "missing or extra evaluation trials"}
            arms[arm] = rows

        correctness = {}
        for arm in ("candidate", "incumbent"):
            rows = arms[arm]
            correctness[arm] = sum(
                rows[key]["decision"] == case["expected"]
                for key, case in ids.items()
            )
        unsafe = sum(
            arms["candidate"][key]["decision"] == "PROPOSE"
            and case["expected"] == "ABSTAIN"
            for key, case in ids.items()
        )
        control_failures = sum(
            case["negative_control"]
            and arms["candidate"][key]["decision"] != "ABSTAIN"
            for key, case in ids.items()
        )
        cand_cost = sum(
            row["probe_cost_units"] for row in arms["candidate"].values()
        )
        base_cost = sum(
            row["probe_cost_units"] for row in arms["incumbent"].values()
        )
        cand_p95 = _p95([
            row["latency_ms"] for row in arms["candidate"].values()
        ])
        base_p95 = _p95([
            row["latency_ms"] for row in arms["incumbent"].values()
        ])
        n = len(ids)
        delta = (correctness["candidate"] - correctness["incumbent"]) / n
        # Paired correctness differences are in [-1, +1].
        margin = math.sqrt(2 * math.log(40) / n)
        interval = [max(-1., delta - margin), min(1., delta + margin)]
        metrics = {
            "pairs": n, "negative_controls": controls,
            "candidate_correct": correctness["candidate"],
            "incumbent_correct": correctness["incumbent"],
            "candidate_unsafe": unsafe,
            "negative_control_failures": control_failures,
            "candidate_probe_cost_units": cand_cost,
            "incumbent_probe_cost_units": base_cost,
            "candidate_p95_latency_ms": cand_p95,
            "incumbent_p95_latency_ms": base_p95,
            "paired_correctness_delta": round(delta, 4),
            "conservative_95pct_interval": [round(x, 4) for x in interval],
        }
        safety = unsafe == control_failures == 0
        nonregression = correctness["candidate"] >= correctness["incumbent"]
        cost_ok = cand_cost <= base_cost
        latency_ok = cand_p95 <= base_p95
        # "PASS" is an OFFLINE correctness/cost result only. This code has
        # no independent effect oracle or release-owner signature.
        if not safety or not nonregression or not cost_ok or not latency_ok:
            state = "OFFLINE_FAIL"
        elif interval[0] <= 0:
            state = "INCONCLUSIVE"
        else:
            state = "OFFLINE_PASS"
        return {**report, "state": state, "metrics": metrics}
    except (KeyError, TypeError, ValueError, AttributeError):
        return {**report, "reason": "malformed evaluation input"}


def _unique_pairs(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_nonstandard_number(value: str):
    raise ValueError("non-standard numeric literal in evaluation input")


def read_json(path: str) -> dict:
    file = Path(path)
    if file.stat().st_size > MAX_BYTES:
        raise ValueError("evaluation file exceeds fixed size bound")
    value = json.loads(
        file.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_pairs,
        parse_constant=_reject_nonstandard_number,
    )
    if not isinstance(value, dict):
        raise ValueError("evaluation document must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline E5 paired holdout verifier")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--oracle", required=True)
    parser.add_argument("--trials", required=True)
    args = parser.parse_args()
    report = evaluate_heldout(
        read_json(args.manifest), read_json(args.oracle), read_json(args.trials),
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    if report["state"] == "INVALID":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
