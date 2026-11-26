import type { Investigation } from "./api";

function humanize(value: string): string {
  return value.toLowerCase().replaceAll("_", " ");
}

const causeLabels: Record<string, string> = {
  simulated_target_unhealthy: "Simulator target may be unhealthy",
  no_simulated_fault: "No simulator fault observed",
};

export function InvestigationPanel({ report, stale }: { report: Investigation; stale: boolean }) {
  const { probe_budget: budget, latest_assessment: assessment, recovery } = report;
  const outcome = recovery?.latest_outcome_label;
  const witnessTime = recovery?.admission_to_recorded_witness_ms;
  const effect = report.latest_effect_state;
  const unresolved = effect === "UNKNOWN";
  const adverse = outcome === "FAILED" || outcome === "INCONCLUSIVE" || outcome === "DISPUTED";

  return <div className="audit" aria-labelledby="investigation-title">
    <h3 id="investigation-title">Investigation</h3>
    {stale && <p className="muted">Last fetched details</p>}
    <dl>
      <dt>Probe budget</dt><dd>{budget.used} used · {budget.remaining} remaining of {budget.limit}</dd>
      <dt>Assessment</dt><dd>{assessment
        ? `Step ${assessment.step_no}: ${humanize(assessment.outcome)} · ${humanize(assessment.reason)}`
        : "Pending"}</dd>
      <dt>Outcome</dt><dd>{outcome
        ? `${humanize(outcome)} · revision ${recovery?.latest_outcome_revision}`
        : "No outcome recorded"}</dd>
      {witnessTime != null && <><dt>Simulator witness</dt><dd>{(witnessTime / 1000).toFixed(2)} s after admission</dd></>}
      <dt>Effect receipt</dt><dd>{effect ?? "No effect recorded"}</dd>
    </dl>
    {(adverse || unresolved) && <p role="status" className="error">
      {unresolved ? "Effect is UNKNOWN. Independent confirmation is pending." : `Latest outcome: ${humanize(outcome!)}.`}
    </p>}
    {report.steps.length === 0 && <p className="muted">No investigation steps recorded.</p>}
    {report.steps_truncated && <p className="muted">Showing the latest 20 steps.</p>}
    {report.steps.map(step => <div className="audit-item" key={step.step_no}>
      <strong>Step {step.step_no}</strong><span>{humanize(step.decision)}</span>
      <p>Probe: {step.probe_id}</p>
      {step.hypotheses.length > 0 && <ul aria-label="Hypotheses and observations">
        {step.hypotheses.map(cause => <li key={cause}>{causeLabels[cause] ?? humanize(cause)}</li>)}
      </ul>}
      {step.evidence.length > 0 && <details>
        <summary>Evidence for step {step.step_no}</summary>
        {step.evidence.map(item => <div key={item.id}>
          <p>{item.source} · {item.validity}</p><code>{item.redacted_excerpt}</code>
        </div>)}
      </details>}
      {step.incomplete_evidence && <p className="muted">Some evidence references are unavailable.</p>}
    </div>)}
  </div>;
}
