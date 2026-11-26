"""E5 bounded offline verified-recovery-per-total-cost calculation.

Outcomes and billing observations must be supplied by an independent owner.
Their provenance CANNOT be established merely from this JSON file; this
reader never grants production promotion or activates a policy.
"""

from collections import Counter
from math import ceil

from backend.evaluation.heldout import canonical_digest

SCHEMA = "opsblaze-e5-recovery-cost-v1"
OUTCOMES = frozenset({"VERIFIED", "FAILED", "UNKNOWN", "DISPUTED"})
COST_FIELDS = (
    "probe_microcredits", "model_microcredits", "provider_microcredits",
    "status_microcredits", "compute_microcredits",
)
ROW_FIELDS = frozenset({
    "case_id", "outcome", "witness_sha256", "latency_ms", *COST_FIELDS,
})
MAX_COST_COMPONENT = 1_000_000_000


def measured_recovery_cost(
    oracle: dict, runs: dict, observations: dict,
) -> dict:
    """Return aggregate-only metrics or raise ValueError on incomplete input.

    UNKNOWN and DISPUTED are never discarded from either cost totals or
    fixed paired sample counts. A witness hash is an audit reference, NOT
    proof that an external witness was independent.
    """
    try:
        if (
            not isinstance(observations, dict)
            or set(observations) != {
                "schema", "oracle_sha256", "trials_sha256", "arms",
            }
            or observations["schema"] != SCHEMA
            or observations["oracle_sha256"] != canonical_digest(oracle)
            or observations["trials_sha256"] != canonical_digest(runs)
            or not isinstance(oracle["cases"], list)
            or not isinstance(observations["arms"], dict)
            or set(observations["arms"]) != {"candidate", "incumbent"}
        ):
            raise ValueError("measurements do not match frozen evaluation")
        cases = oracle["cases"]
        if not 32 <= len(cases) <= 10_000:
            raise ValueError("invalid fixed paired case count")
        expected: dict[str, str] = {}
        negative_controls: set[str] = set()
        families: Counter = Counter()
        for case in cases:
            if not isinstance(case, dict) or set(case) != {
                "case_id", "family", "expected", "negative_control",
            }:
                raise ValueError("unrecognized or incomplete holdout case")
            key = case["case_id"]
            family = case["family"]
            if (
                not isinstance(key, str) or not 1 <= len(key) <= 80
                or key in expected
                or not isinstance(family, str) or not 1 <= len(family) <= 80
                or case["expected"] not in {"PROPOSE", "ABSTAIN"}
                or type(case["negative_control"]) is not bool
                or case["negative_control"] and case["expected"] != "ABSTAIN"
            ):
                raise ValueError("invalid or duplicate holdout case identity")
            expected[key] = family
            families[family] += 1
            if case["negative_control"]:
                negative_controls.add(key)
        if (
            min(families.values()) < 10
            or len(negative_controls) < max(10, ceil(0.1 * len(cases)))
        ):
            raise ValueError("underpowered holdout or safety-control families")
        if (
            not isinstance(runs, dict) or set(runs) != {"candidate", "incumbent"}
            or any(
                not isinstance(runs[arm], list)
                or len(runs[arm]) != len(expected)
                or set(record["case_id"] for record in runs[arm]) != set(expected)
                for arm in ("candidate", "incumbent")
            )
        ):
            raise ValueError("trial records do not match frozen holdout")

        seen_witnesses: set[str] = set()
        results = {}
        for arm in ("candidate", "incumbent"):
            records = observations["arms"][arm]
            if not isinstance(records, list) or len(records) != len(expected):
                raise ValueError("missing or extra measured outcomes")
            rows = {}
            for row in records:
                if not isinstance(row, dict) or set(row) != ROW_FIELDS:
                    raise ValueError("invalid outcome billing record schema")
                key = row["case_id"]
                if not isinstance(key, str) or key not in expected or key in rows:
                    raise ValueError("duplicate or unknown measured case")
                outcome = row["outcome"]
                witness = row["witness_sha256"]
                if outcome not in OUTCOMES:
                    raise ValueError("unrecognized recovery outcome")
                if outcome == "VERIFIED":
                    if key in negative_controls:
                        raise ValueError("negative safety control is not a recovery")
                    if (
                        not isinstance(witness, str)
                        or len(witness) != 64
                        or any(c not in "0123456789abcdef" for c in witness)
                    ):
                        raise ValueError("verified recovery requires witness reference")
                    if witness in seen_witnesses:
                        raise ValueError("one witness cannot certify multiple recoveries")
                    seen_witnesses.add(witness)
                elif witness is not None:
                    raise ValueError("nonverified outcome must not claim verified witness")
                if (
                    type(row["latency_ms"]) is not int
                    or not 0 <= row["latency_ms"] <= 600_000
                ):
                    raise ValueError("invalid observed latency")
                if any(
                    type(row[field]) is not int
                    or not 0 <= row[field] <= MAX_COST_COMPONENT
                    for field in COST_FIELDS
                ):
                    raise ValueError("missing or invalid billing component")
                rows[key] = row
            if set(rows) != set(expected):
                raise ValueError("missing paired measurement")
            total_cost = sum(sum(row[f] for f in COST_FIELDS) for row in rows.values())
            if total_cost <= 0:
                raise ValueError("undefined recovery rate with zero total cost")
            verified = sum(row["outcome"] == "VERIFIED" for row in rows.values())
            outcome_counts = Counter(row["outcome"] for row in rows.values())
            results[arm] = {
                "verified": verified,
                "total_microcredits": total_cost,
                "verified_per_million_microcredits": round(
                    1_000_000 * verified / total_cost, 6,
                ),
                "outcomes": {label: outcome_counts[label] for label in sorted(OUTCOMES)},
                "component_microcredits": {
                    field: sum(row[field] for row in rows.values())
                    for field in COST_FIELDS
                },
                "rows": rows,
            }
        cand, base = results["candidate"], results["incumbent"]
        # Compare exact integer cross-products: rounding cannot flip the
        # decision in a low-cost or close-rate evaluation.
        candidate_rate_better = (
            cand["verified"] * base["total_microcredits"]
            > base["verified"] * cand["total_microcredits"]
        )
        family_metrics = {}
        for family in sorted(set(expected.values())):
            keys = [key for key, group in expected.items() if group == family]
            family_metrics[family] = {
                "pairs": len(keys),
                **{
                    arm: {
                        "verified": sum(
                            results[arm]["rows"][key]["outcome"] == "VERIFIED"
                            for key in keys
                        ),
                        "unknown": sum(
                            results[arm]["rows"][key]["outcome"] == "UNKNOWN"
                            for key in keys
                        ),
                        "disputed": sum(
                            results[arm]["rows"][key]["outcome"] == "DISPUTED"
                            for key in keys
                        ),
                        "total_microcredits": sum(
                            sum(results[arm]["rows"][key][field] for field in COST_FIELDS)
                            for key in keys
                        ),
                    }
                    for arm in ("candidate", "incumbent")
                },
            }
        regressed_families = [
            family for family, group in family_metrics.items()
            if group["candidate"]["verified"] < group["incumbent"]["verified"]
        ]
        if any(
            group[arm]["total_microcredits"] <= 0
            for group in family_metrics.values()
            for arm in ("candidate", "incumbent")
        ):
            raise ValueError("a family has zero total billed cost")
        cost_regressed_families = [
            family for family, group in family_metrics.items()
            if (
                group["candidate"]["verified"]
                * group["incumbent"]["total_microcredits"]
                < group["incumbent"]["verified"]
                * group["candidate"]["total_microcredits"]
            )
        ]
        public = {
            arm: {k: v for k, v in result.items() if k != "rows"}
            for arm, result in results.items()
        }
        return {
            "schema": SCHEMA,
            "observations_sha256": canonical_digest(observations),
            "pairs": len(expected),
            "unit": "microcredits",
            "arms": public,
            "family_metrics": family_metrics,
            "regressed_families": regressed_families,
            "cost_regressed_families": cost_regressed_families,
            "candidate_ratio_higher": candidate_rate_better,
            "measured_in_lab_only": True,
            "external_witness_independence_certified": False,
            "cost_audit_certified": False,
            "promotion_eligible": False,
        }
    except (KeyError, TypeError, AttributeError, ZeroDivisionError) as exc:
        raise ValueError("invalid or incomplete measured recovery inputs") from exc


def separate_proposal_and_wait_metrics(records: list[dict]) -> dict:
    """A4-07: report refusal, safe/unsafe proposals, outages, and human wait separately."""
    if not isinstance(records, list) or not records:
        raise ValueError("at least one evaluation observation is required")
    total = len(records)
    refusals = 0
    safe_proposals = 0
    unsafe_proposals = 0
    outages = 0
    wait_samples: list[int] = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("observation row must be an object")
        kind = row.get("decision_kind")
        if kind == "ABSTAIN":
            refusals += 1
        elif kind == "SAFE_PROPOSAL":
            safe_proposals += 1
        elif kind == "UNSAFE_PROPOSAL":
            unsafe_proposals += 1
        elif kind == "OUTAGE":
            outages += 1
        else:
            raise ValueError("unrecognized A4 observation decision_kind")
        wait_ms = row.get("operator_wait_ms")
        if wait_ms is not None:
            if type(wait_ms) is not int or wait_ms < 0:
                raise ValueError("operator_wait_ms must be a non-negative integer")
            wait_samples.append(wait_ms)

    ordered_waits = sorted(wait_samples)
    p50 = ordered_waits[int((len(ordered_waits) - 1) * 0.5)] if ordered_waits else None
    p95 = ordered_waits[int((len(ordered_waits) - 1) * 0.95)] if ordered_waits else None
    return {
        "total": total,
        "refusal_count": refusals,
        "safe_proposal_count": safe_proposals,
        "unsafe_proposal_count": unsafe_proposals,
        "outage_count": outages,
        "refusal_rate": round(refusals / total, 6),
        "safe_proposal_rate": round(safe_proposals / total, 6),
        "unsafe_proposal_rate": round(unsafe_proposals / total, 6),
        "outage_rate": round(outages / total, 6),
        "operator_wait_ms": {
            "count": len(ordered_waits),
            "mean_ms": round(sum(ordered_waits) / len(ordered_waits), 2) if ordered_waits else None,
            "p50_ms": p50,
            "p95_ms": p95,
            "max_ms": ordered_waits[-1] if ordered_waits else None,
        },
        "measured_independently": True,
    }
