const evidenceGuidance = "Obtain fresh evidence for this target and compare it with the recorded observations.";

const nextStep = new Map([
  ["BUDGET_EXHAUSTED", "Review the recorded observations and follow the incident runbook for escalation."],
  ["TOOL_BLOCKED", "Ask the incident owner to review the probe's authorization and target scope."],
  ["TOOL_UNAVAILABLE", "Check probe and worker availability, then obtain a current read-only observation."],
  ["MODEL_UNCERTAINTY", "Review the linked evidence and obtain an independent read-only observation."],
  ["TARGET_ALREADY_HEALTHY", "Confirm target health with an independent observation before closing the incident."],
  ["VERSION_CHANGED", "Compare the current target version with the recorded version before requesting review."],
  ["MISSING_EVIDENCE", evidenceGuidance],
  ["INVALID_REFERENCE", evidenceGuidance],
  ["INVALID_EVIDENCE", evidenceGuidance],
  ["HASH_MISMATCH", evidenceGuidance],
  ["STALE_EVIDENCE", evidenceGuidance],
  ["UNLINKED_EVIDENCE", evidenceGuidance],
  ["INVALID_OBSERVATION", evidenceGuidance],
]);

export function handoffGuidance(reason: string | undefined): string {
  return nextStep.get(reason ?? "") ?? "Review the recorded evidence and follow the incident runbook.";
}

const fallbackReason = new Map([
  ["UNCONFIRMED", "The advisory request could not be confirmed."],
  ["PROVIDER_UNAVAILABLE", "The advisory provider did not return usable guidance."],
  ["BUDGET_DENIED", "The advisory token budget was unavailable."],
  ["DISABLED", "The advisory provider was disabled."],
]);

export function advisoryFallbackMessage(reason: string): string {
  return fallbackReason.get(reason) ?? "The advisory result was unavailable.";
}
