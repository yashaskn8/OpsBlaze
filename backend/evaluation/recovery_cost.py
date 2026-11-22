"""E5 bounded offline verified-recovery-per-total-cost calculation.

Outcomes and billing observations must be supplied by an independent owner.
Their provenance CANNOT be established merely from this JSON file; this
reader never grants production promotion or activates a policy.
"""

from collections import Counter

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
        expected = {row["case_id"]: row["family"] for row in cases}
        negative_controls = {
            row["case_id"] for row in cases if row["negative_control"]
        }
        if len(expected) != len(cases) or not 32 <= len(cases) <= 10_000:
            raise ValueError("invalid fixed paired case identifiers")

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
            "candidate_ratio_higher": candidate_rate_better,
            "measured_in_lab_only": True,
            "external_witness_independence_certified": False,
            "cost_audit_certified": False,
            "promotion_eligible": False,
        }
    except (KeyError, TypeError, AttributeError, ZeroDivisionError) as exc:
        raise ValueError("invalid or incomplete measured recovery inputs") from exc
