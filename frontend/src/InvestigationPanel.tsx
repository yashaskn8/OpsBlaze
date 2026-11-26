import type { Investigation } from "./api";
import { advisoryFallbackMessage, handoffGuidance } from "./investigationGuidance";

function humanize(value: string): string {
  return value.toLowerCase().replaceAll("_", " ");
}

const causeLabels: Record<string, string> = {
  simulated_target_unhealthy: "Simulator target may be unhealthy",
  no_simulated_fault: "No simulator fault observed",
};

export function InvestigationPanel({ report, stale }: { report: Investigation; stale: boolean }) {
  const { probe_budget: budget, latest_assessment: assessment, recovery } = report;
  const handoff = report.incident.status_projection === "NEEDS_REVIEW";
  const handoffReason = assessment?.outcome === "ABSTAINED" ? assessment.reason : undefined;
  const fallback = report.advisory_fallback_reason;
  const outcome = recovery?.latest_outcome_label;
  const witnessTime = recovery?.admission_to_recorded_witness_ms;
  const effect = report.latest_effect_state;
  const unresolved = effect === "UNKNOWN";
  const pending = !outcome && (report.incident.status_projection === "EXECUTING"
    || unresolved || effect === "SENT" || effect === "VERIFIED");
  const adverse = outcome === "FAILED" || outcome === "INCONCLUSIVE" || outcome === "DISPUTED";

  return <div className="audit" aria-labelledby="investigation-title">
    <h3 id="investigation-title">Investigation</h3>
    {stale && <p className="muted">Last fetched details</p>}
    {handoff && <div className="audit-item" role="status">
      <strong>{stale ? "Last recorded handoff" : "Operator handoff"}</strong>
      <p>{handoffReason ? humanize(handoffReason) : "Review required"}</p>
      <p>{handoffGuidance(handoffReason)}</p>
    </div>}
    {fallback && <p className="muted" role="status">
      <strong>Runbook fallback.</strong> {advisoryFallbackMessage(fallback)}
    </p>}
    <dl>
      <dt>Probe budget</dt><dd>{budget.used} used · {budget.remaining} remaining of {budget.limit}</dd>
      <dt>Assessment</dt><dd>{assessment
        ? `Step ${assessment.step_no}: ${humanize(assessment.outcome)} · ${humanize(assessment.reason)}`
        : "Pending"}</dd>
      <dt>Effect receipt</dt><dd>{effect ?? "No effect recorded"}</dd>
    </dl>
    {unresolved && <p role="status" className="error">Effect is UNKNOWN. Independent confirmation is pending.</p>}
    <section id="investigation-outcome" tabIndex={-1} aria-labelledby="investigation-outcome-title" className="audit-item">
      <h4 id="investigation-outcome-title">Outcome record</h4>
      <p role="status" className={adverse ? "error" : "muted"}>{outcome
        ? `${humanize(outcome)} · revision ${recovery?.latest_outcome_revision}`
        : pending ? "Pending independent confirmation" : "No outcome recorded"}</p>
      {outcome === "DISPUTED" && <p>Review the recorded source and obtain an independent observation before closing the incident.</p>}
      {recovery?.latest_outcome_id && <dl>
        <dt>Record ID</dt><dd className="mono">{recovery.latest_outcome_id}</dd>
        {recovery.latest_outcome_witness_source && <>
          <dt>Source</dt><dd>{humanize(recovery.latest_outcome_witness_source)}</dd>
        </>}
        {recovery.latest_outcome_recorded_at && <>
          <dt>Recorded</dt><dd>{new Date(recovery.latest_outcome_recorded_at).toLocaleString()}</dd>
        </>}
        {recovery.latest_outcome_observed_version != null && <>
          <dt>Observed target version</dt><dd>{recovery.latest_outcome_observed_version}</dd>
        </>}
        {witnessTime != null && <><dt>Simulator witness</dt><dd>{(witnessTime / 1000).toFixed(2)} s after admission</dd></>}
      </dl>}
    </section>
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
          <p>{item.source} · {item.freshness === "EXPIRED" ? item.freshness : item.validity}</p><code>{item.redacted_excerpt}</code>
        </div>)}
      </details>}
      {step.incomplete_evidence && <p className="muted">Some evidence references are unavailable.</p>}
    </div>)}
  </div>;
}
