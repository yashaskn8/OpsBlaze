# Runbook: S1 Time-Sealed Paired Decision Replay & Statistical Evaluation

**Product:** OpsBlaze / TITAN-20  
**Feature:** S1 — Time-Sealed Paired Decision Replay  
**Target Roles:** Platform Engineer, AI Evaluation Owner, SRE Lead, QA Automation Architect  

---

## 1. Overview & Evaluation Principles

Feature S1 provides a rigorous, time-sealed paired decision replay and statistical evaluation framework. It evaluates proposed candidate policies or agent planner strategies against frozen baselines on identical diagnostic fixtures.

### Key Invariants
1. **Time-Sealed Isolation:** Diagnostic fixtures and hidden root-cause labels are cryptographically sealed in an immutable manifest (`EvaluationManifest`) with deterministic hashing (`manifest_sha256`).
2. **Paired Fixture Replay:** Every incident fixture is presented identically to:
   - Strategy A: Static Runbook (inflexible, prone to false alarms)
   - Strategy B: Cheapest-First Heuristic (stops after shallow probes)
   - Strategy C: A1 Adaptive Probe Planner (discriminating probes + calibrated abstention)
3. **Independent Oracle Adjudication:** Adjudication is conducted by an independent oracle (`IndependentOracle`) comparing candidate decisions against hidden ground-truth labels.
4. **Strict Negative Control Gate:** Negative controls (benign transients, recovered jitter, expected failovers) require a NO-OP decision. A candidate that executes any mutation on a negative control fails the quality gate immediately (0% false-positive tolerance).
5. **Standard-Library Statistical Significance:**
   - **McNemar's Paired Test:** Evaluates whether differences in decision accuracy across paired fixtures are statistically significant ($p < 0.05$).
   - **Wilcoxon Signed-Rank Test:** Evaluates whether differences in cost/token distribution are statistically significant without normality assumptions.

---

## 2. Standard Evaluation Execution

### 2.1 Running the Benchmark Suite
To execute a reproducible evaluation run:
```bash
poetry run python scripts/stage6_evaluation_benchmark.py
```

### 2.2 Inspecting Stored Evaluation Trials in PostgreSQL
All trials are permanently recorded in PostgreSQL:
```sql
-- Query manifest metadata
SELECT id, title, fixture_family, strategy_version, random_seed, split, created_at
FROM evaluation_manifest
ORDER BY created_at DESC
LIMIT 1;

-- Query trial summary by strategy
SELECT
    strategy_name,
    COUNT(*) AS total_trials,
    SUM(CASE WHEN decision_correct THEN 1 ELSE 0 END)::float / COUNT(*) AS accuracy,
    AVG(probes_count) AS avg_probes,
    AVG(cost_micros) AS avg_cost_micros
FROM evaluation_trial
WHERE manifest_id = 'd87a0d45-ec1e-4616-b158-e9e3aa4469d2'
GROUP BY strategy_name;
```

---

## 3. Interpreting Statistical Test Results

### 3.1 McNemar Paired Accuracy Test
- $n_{01}$: Number of pairs where Baseline was incorrect and Candidate was correct.
- $n_{10}$: Number of pairs where Baseline was correct and Candidate was incorrect.
- If $n_{01} + n_{10} < 25$, exact binomial calculation is applied; otherwise continuity-corrected $\chi^2$ is used:
  $$\chi^2 = \frac{(|n_{01} - n_{10}| - 1)^2}{n_{01} + n_{10}}$$
- **Decision:** A candidate policy is accepted only if $p < 0.05$ and $n_{01} > n_{10}$.

### 3.2 Wilcoxon Signed-Rank Cost Test
- Non-parametric paired difference test on costs $d_i = \text{cost}_{A, i} - \text{cost}_{C, i}$.
- Ranks absolute differences $|d_i|$ and computes rank sums $W^+$ and $W^-$.
- Tests whether the candidate significantly reduces exploration costs.

### 3.3 Negative Control Quarantine Trigger
If `negative_control_pass_rate < 1.0`:
- The candidate strategy is automatically **REJECTED** and quarantined.
- Prompts, probe selection thresholds, and abstention margins must be reviewed before candidate re-evaluation.
