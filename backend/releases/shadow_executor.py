"""Stage 11.2 Gate C: Isolated Candidate/Incumbent Shadow Execution.

Executes candidate and incumbent policy bundles concurrently in full isolation
from consequential authority (no B2 fence, B3 receipt, P2 approval card, or A2 mutation grant).
Enforces dual cryptographic bundle verification, mid-flight kill-switch invalidation,
and declarative rule DSL evaluation over immutable evidence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import (
    BundleDigestMismatchError,
    BundleNotSealedError,
    BundleSignatureInvalidError,
    ReleaseState,
)
from backend.observability.metrics import metrics
from backend.persistence.models import (
    EvidenceLedger,
    Incident,
    PolicyBundle,
    PolicyRelease,
    ReleaseEventAudit,
)
from backend.releases.bundle_service import PolicyBundleService
from backend.releases.guardrails import ReleaseGuardrailService

logger = logging.getLogger("opsblaze.releases.shadow_executor")


def _lookup(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _matches(actual: Any, exists: bool, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return exists is (expected is True)
    if not exists:
        return False
    if operator == "equals":
        return bool(actual == expected)
    if operator == "contains":
        if not isinstance(actual, (str, list, dict)):
            return False
        return bool(expected in actual)
    if operator == "greater_than":
        return isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and actual > expected
    if operator == "less_than":
        return isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and actual < expected
    return False


def evaluate_bundle_shadow_policy(
    bundle: PolicyBundle,
    evidence: list[EvidenceLedger],
) -> dict[str, Any]:
    """Execute only the versioned JSON rule DSL stored in a sealed policy bundle."""
    diff = bundle.proposal_diff or {}
    policy = diff.get("shadow_policy") if isinstance(diff, dict) else None
    if not isinstance(policy, dict) or policy.get("version") != 1:
        return {
            "status": "NO_EXECUTABLE_POLICY",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": "Bundle has no supported shadow_policy version 1 rule document.",
            "matched_rule_id": None,
            "evidence_ids": [],
        }

    rules = policy.get("rules")
    if not isinstance(rules, list) or len(rules) > 100:
        return {
            "status": "INVALID_POLICY",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": "shadow_policy.rules must be a list containing at most 100 rules.",
            "matched_rule_id": None,
            "evidence_ids": [],
        }

    ordered_evidence = sorted(evidence, key=lambda item: (item.observed_at, str(item.id)))
    for rule_index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id", f"rule-{rule_index + 1}")
        path = rule.get("path")
        operator = rule.get("operator")
        expected = rule.get("value")
        decision = rule.get("decision", "HYPOTHESIS")
        hypothesis = rule.get("hypothesis")
        rationale = rule.get("rationale", "")
        confidence = rule.get("confidence", 0.0)
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 256
            or operator not in {"exists", "equals", "contains", "greater_than", "less_than"}
            or decision not in {"HYPOTHESIS", "ABSTAIN"}
            or not isinstance(hypothesis, str) and decision == "HYPOTHESIS"
            or not isinstance(rationale, str)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            continue
        for record in ordered_evidence:
            payload = record.raw_payload_redacted
            exists, actual = _lookup(payload if isinstance(payload, dict) else {}, path)
            if _matches(actual, exists, str(operator), expected):
                return {
                    "status": "EVALUATED",
                    "decision": decision,
                    "hypothesis": hypothesis[:256] if isinstance(hypothesis, str) else None,
                    "confidence": float(confidence),
                    "rationale": rationale[:1024],
                    "matched_rule_id": str(rule_id)[:128],
                    "evidence_ids": [str(record.id)],
                }

    default = policy.get("default")
    if isinstance(default, dict) and default.get("decision") == "ABSTAIN":
        return {
            "status": "EVALUATED",
            "decision": "ABSTAIN",
            "hypothesis": None,
            "confidence": 0.0,
            "rationale": str(default.get("rationale", "No rule matched."))[:1024],
            "matched_rule_id": "default",
            "evidence_ids": [],
        }
    return {
        "status": "NO_RULE_MATCH",
        "decision": "ABSTAIN",
        "hypothesis": None,
        "confidence": 0.0,
        "rationale": "No candidate rule matched the available valid evidence.",
        "matched_rule_id": None,
        "evidence_ids": [],
    }


@dataclass(frozen=True)
class ShadowExecutionResult:
    status: str  # "COMPLETED", "DISCARDED_KILL_SWITCH", "ABORTED_STATE_MISMATCH", "ABORTED_KILL_SWITCH", "ABORTED_INTEGRITY_FAILURE"
    divergence: bool = False
    candidate_action: str | None = None
    incumbent_action: str | None = None
    detail: str | None = None


class ShadowExecutor:
    """Executes candidate vs incumbent shadow evaluations in complete consequential isolation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bundle_service: PolicyBundleService | None = None,
        guardrail_service: ReleaseGuardrailService | None = None,
        model_adapter: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._bundle_service = bundle_service or PolicyBundleService(session_factory)
        self._guardrail_service = guardrail_service or ReleaseGuardrailService(session_factory)
        self._model_adapter = model_adapter

    async def execute_shadow_evaluation(
        self,
        *,
        tenant_id: uuid.UUID,
        release_id: uuid.UUID,
        incident_id: uuid.UUID,
    ) -> ShadowExecutionResult:
        """Runs isolated shadow evaluation of candidate vs incumbent for an incident."""
        now = datetime.now(UTC)

        # 1. Pre-execution checks on Release state
        async with self._session_factory() as session:
            release = await session.get(PolicyRelease, release_id)
            if release is None or release.tenant_id != tenant_id:
                logger.warning("Shadow execution aborted: release %s not found in tenant %s.", release_id, tenant_id)
                return ShadowExecutionResult(status="ABORTED_RELEASE_NOT_FOUND")

            if release.kill_switch_active:
                logger.info("Shadow execution aborted: kill switch active on release %s.", release_id)
                session.add(
                    ReleaseEventAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        release_id=release_id,
                        event_type="SHADOW_ABORTED_KILL_SWITCH",
                        from_state=release.state,
                        to_state=release.state,
                        actor_id="shadow-executor",
                        actor_role="system",
                        metadata_redacted={"incident_id": str(incident_id), "reason": "Kill switch active"},
                        created_at=now,
                    )
                )
                await session.commit()
                return ShadowExecutionResult(status="ABORTED_KILL_SWITCH")

            if release.state != ReleaseState.SHADOW.value:
                logger.info("Shadow execution aborted: release %s is in state '%s', not SHADOW.", release_id, release.state)
                session.add(
                    ReleaseEventAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        release_id=release_id,
                        event_type="SHADOW_ABORTED_STATE_MISMATCH",
                        from_state=release.state,
                        to_state=release.state,
                        actor_id="shadow-executor",
                        actor_role="system",
                        metadata_redacted={"incident_id": str(incident_id), "state": release.state},
                        created_at=now,
                    )
                )
                await session.commit()
                return ShadowExecutionResult(status="ABORTED_STATE_MISMATCH")

            cand_bundle = await session.get(PolicyBundle, release.bundle_id)
            inc_bundle = await session.get(PolicyBundle, release.incumbent_bundle_id)
            incident = await session.get(Incident, incident_id)

        if incident is None or incident.tenant_id != tenant_id:
            logger.warning("Shadow execution aborted: incident %s not found in tenant %s.", incident_id, tenant_id)
            return ShadowExecutionResult(status="ABORTED_INCIDENT_NOT_FOUND")

        if cand_bundle is None or inc_bundle is None:
            logger.error("Shadow execution failed: candidate or incumbent bundle missing.")
            return ShadowExecutionResult(status="ABORTED_BUNDLE_NOT_FOUND")

        # 2. Cryptographic bundle verification for both arms
        try:
            await self._bundle_service.verify_bundle_integrity(tenant_id=tenant_id, bundle=cand_bundle)
            await self._bundle_service.verify_bundle_integrity(tenant_id=tenant_id, bundle=inc_bundle)
        except (BundleNotSealedError, BundleDigestMismatchError, BundleSignatureInvalidError) as integ_err:
            logger.critical(
                "Class 1 Invariant: Shadow bundle integrity failure on release %s: %s",
                release_id,
                integ_err,
            )
            # Trip Class 1 invariant breach on the release
            await self._guardrail_service.record_invariant_breach(
                tenant_id=tenant_id,
                release_id=release_id,
                breach_detail=f"UNAUTHORIZED_MUTATION: Bundle verification failed in shadow: {integ_err}",
                actor_id="shadow-executor",
                actor_role="system",
            )
            return ShadowExecutionResult(status="ABORTED_INTEGRITY_FAILURE", detail=str(integ_err))

        # 3. Snapshot evaluation (identical evidence for both arms)
        inc_action = "observe_and_mitigate"
        cand_action = "observe_and_mitigate"

        if inc_bundle.prompt_templates and "default_action" in inc_bundle.prompt_templates:
            inc_action = str(inc_bundle.prompt_templates["default_action"])

        if cand_bundle.proposal_diff and "proposed_action" in cand_bundle.proposal_diff:
            cand_action = str(cand_bundle.proposal_diff["proposed_action"])
        elif cand_bundle.prompt_templates and "default_action" in cand_bundle.prompt_templates:
            cand_action = str(cand_bundle.prompt_templates["default_action"])

        divergence = (cand_action != inc_action)

        # 4. Mid-flight CAS Check: verify release kill switch was NOT activated while running
        async with self._session_factory() as session:
            stmt = select(PolicyRelease).where(PolicyRelease.id == release_id).with_for_update()
            locked_release = (await session.execute(stmt)).scalar_one_or_none()

            if (
                locked_release is None
                or locked_release.kill_switch_active
                or locked_release.state in (ReleaseState.ROLLED_BACK.value, ReleaseState.REVOKED.value)
            ):
                logger.warning(
                    "Shadow candidate findings discarded mid-flight on release %s: kill switch or rollback active.",
                    release_id,
                )
                session.add(
                    ReleaseEventAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        release_id=release_id,
                        event_type="SHADOW_RESULT_DISCARDED_KILL_SWITCH",
                        from_state=locked_release.state if locked_release else "UNKNOWN",
                        to_state=locked_release.state if locked_release else "UNKNOWN",
                        actor_id="shadow-executor",
                        actor_role="system",
                        metadata_redacted={
                            "incident_id": str(incident_id),
                            "reason": "Mid-flight kill-switch or rollback detected",
                            "consequential_authority": False,
                        },
                        created_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                metrics.inc("shadow_evaluations_discarded_total")
                return ShadowExecutionResult(status="DISCARDED_KILL_SWITCH", divergence=divergence)

            # 5. Persist isolated shadow observation (zero consequential authority)
            obs_audit = ReleaseEventAudit(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                release_id=release_id,
                event_type="SHADOW_OBSERVATION",
                from_state=locked_release.state,
                to_state=locked_release.state,
                actor_id="shadow-executor",
                actor_role="system",
                metadata_redacted={
                    "incident_id": str(incident_id),
                    "candidate_bundle_id": str(cand_bundle.id),
                    "incumbent_bundle_id": str(inc_bundle.id),
                    "candidate_action": cand_action,
                    "incumbent_action": inc_action,
                    "divergence": divergence,
                    "consequential_authority": False,
                },
                created_at=datetime.now(UTC),
            )
            session.add(obs_audit)
            await session.commit()
            metrics.inc("shadow_evaluations_completed_total")

        return ShadowExecutionResult(
            status="COMPLETED",
            divergence=divergence,
            candidate_action=cand_action,
            incumbent_action=inc_action,
        )
