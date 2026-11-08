"""Statistical tests for OpsBlaze Feature S1 — Paired Decision Replay.

Implements exact and asymptotic McNemar test and Wilcoxon signed-rank test
using standard library `math` without requiring external heavy scientific packages.
"""

from __future__ import annotations

import math
from typing import Any


def mcnemar_test(
    baseline_correct: list[bool], candidate_correct: list[bool]
) -> dict[str, Any]:
    """Calculate McNemar's paired test for binary classification outcomes.

    Parameters
    ----------
    baseline_correct : list[bool]
        Outcomes for baseline strategy (True if correct, False otherwise).
    candidate_correct : list[bool]
        Outcomes for candidate strategy (True if correct, False otherwise).

    Returns
    -------
    dict[str, Any]
        Dictionary with contingency table counts, chi2 statistic, p-value, and significance.
    """
    if len(baseline_correct) != len(candidate_correct):
        raise ValueError("Baseline and candidate outcomes must have identical length")
    if not baseline_correct:
        raise ValueError("Cannot perform statistical test on empty samples")

    n00 = 0  # both incorrect
    n01 = 0  # baseline incorrect, candidate correct
    n10 = 0  # baseline correct, candidate incorrect
    n11 = 0  # both correct

    for b, c in zip(baseline_correct, candidate_correct, strict=True):
        if not b and not c:
            n00 += 1
        elif not b and c:
            n01 += 1
        elif b and not c:
            n10 += 1
        else:
            n11 += 1

    discordant = n01 + n10

    if discordant == 0:
        return {
            "n00": n00,
            "n01": n01,
            "n10": n10,
            "n11": n11,
            "statistic": 0.0,
            "p_value": 1.0,
            "significant_at_05": False,
            "method": "exact_binomial",
        }

    # Small sample exact binomial test (discordant < 25)
    if discordant < 25:
        k = min(n01, n10)
        # Sum of binomial probabilities for k' <= k under p=0.5
        cdf = sum(math.comb(discordant, i) for i in range(k + 1)) * (0.5**discordant)
        p_val = min(1.0, 2.0 * cdf)
        return {
            "n00": n00,
            "n01": n01,
            "n10": n10,
            "n11": n11,
            "statistic": float(discordant),
            "p_value": round(p_val, 6),
            "significant_at_05": p_val < 0.05,
            "method": "exact_binomial",
        }

    # Continuity-corrected Chi-Square test (discordant >= 25)
    chi2 = ((abs(n01 - n10) - 1.0) ** 2) / discordant
    # Survival function for Chi2 with df=1 is erfc(sqrt(chi2 / 2))
    p_val = math.erfc(math.sqrt(chi2 / 2.0))
    return {
        "n00": n00,
        "n01": n01,
        "n10": n10,
        "n11": n11,
        "statistic": round(chi2, 4),
        "p_value": round(p_val, 6),
        "significant_at_05": p_val < 0.05,
        "method": "chi2_continuity_corrected",
    }


def wilcoxon_signed_rank_test(
    x: list[float], y: list[float], zero_method: str = "wilcox"
) -> dict[str, Any]:
    """Calculate Wilcoxon signed-rank test for paired continuous metrics.

    Parameters
    ----------
    x : list[float]
        Metrics for baseline strategy (e.g. costs or steps).
    y : list[float]
        Metrics for candidate strategy (e.g. costs or steps).
    zero_method : str
        Method for treating zero differences ('wilcox' discards zero diffs).

    Returns
    -------
    dict[str, Any]
        Dictionary with W statistic, rank sums, z-score, p-value, and significance.
    """
    if len(x) != len(y):
        raise ValueError("Metric sample lists must have identical length")
    if not x:
        raise ValueError("Cannot perform statistical test on empty samples")

    diffs = [a - b for a, b in zip(x, y, strict=True)]
    non_zero = [d for d in diffs if abs(d) > 1e-9]
    n = len(non_zero)

    if n == 0:
        return {
            "n": 0,
            "w_statistic": 0.0,
            "w_plus": 0.0,
            "w_minus": 0.0,
            "z_score": 0.0,
            "p_value": 1.0,
            "significant_at_05": False,
        }

    # Rank absolute differences
    abs_diffs = sorted([(abs(d), idx, d) for idx, d in enumerate(non_zero)], key=lambda t: t[0])
    ranks: list[float] = [0.0] * n

    # Assign ranks with tie averaging
    i = 0
    while i < n:
        j = i
        while j < n and abs(abs_diffs[j][0] - abs_diffs[i][0]) < 1e-9:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j

    w_plus = 0.0
    w_minus = 0.0
    for (_, _, orig_d), r in zip(abs_diffs, ranks, strict=True):
        if orig_d > 0:
            w_plus += r
        else:
            w_minus += r

    w_stat = min(w_plus, w_minus)

    # Normal approximation for Wilcoxon signed rank test
    mean_w = (n * (n + 1)) / 4.0
    var_w = (n * (n + 1) * (2 * n + 1)) / 24.0

    if var_w <= 0:
        z = 0.0
        p_val = 1.0
    else:
        # Continuity correction
        diff_from_mean = w_stat - mean_w
        if diff_from_mean < 0:
            z = (diff_from_mean + 0.5) / math.sqrt(var_w)
        elif diff_from_mean > 0:
            z = (diff_from_mean - 0.5) / math.sqrt(var_w)
        else:
            z = 0.0

        p_val = math.erfc(abs(z) / math.sqrt(2.0))

    return {
        "n": n,
        "w_statistic": round(w_stat, 2),
        "w_plus": round(w_plus, 2),
        "w_minus": round(w_minus, 2),
        "z_score": round(z, 4),
        "p_value": round(p_val, 6),
        "significant_at_05": p_val < 0.05,
    }



def wilson_score_interval(
    successes: int, total: int, confidence: float = 0.95
) -> tuple[float, float]:
    """Calculate the Wilson score confidence interval for a binomial proportion.

    Parameters
    ----------
    successes : int
        Number of successful outcomes (k).
    total : int
        Total number of trials (n).
    confidence : float, optional
        Confidence level, defaults to 0.95.

    Returns
    -------
    tuple[float, float]
        Tuple of (lower_bound, upper_bound) in the range [0.0, 1.0].
    """
    if total <= 0:
        return 0.0, 1.0
    if successes < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")

    # Map standard confidence levels to two-tailed normal quantiles
    z_table = {
        0.90: 1.6448536269514722,
        0.95: 1.959963984540054,
        0.98: 2.3263478740408408,
        0.99: 2.5758293035489004,
    }
    z = z_table.get(round(confidence, 2), 1.959963984540054)

    p_hat = successes / total
    z2 = z * z
    denom = 1.0 + (z2 / total)
    center = (p_hat + (z2 / (2.0 * total))) / denom
    spread = (z / denom) * math.sqrt(
        (p_hat * (1.0 - p_hat) / total) + (z2 / (4.0 * total * total))
    )

    lower = max(0.0, round(center - spread, 4))
    upper = min(1.0, round(center + spread, 4))
    return lower, upper
