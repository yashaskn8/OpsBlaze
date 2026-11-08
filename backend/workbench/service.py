"""Service for OpsBlaze Feature P1 — Live Investigation Workbench."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.approvals.service import _to_response as approval_to_response
from backend.domain.approval import ApprovalResponse, ApprovalState
from backend.domain.incident import IncidentNotFoundError, IncidentStatus
from backend.domain.workbench import (
    AbstentionDetail,
    BudgetHeadroom,
    EvidenceItem,
    HypothesisItem,
    InvestigationStepItem,
    ToolCallItem,
    WorkbenchSnapshotResponse,
)
from backend.persistence.models import (
    ApprovalRequest,
    BudgetLedger,
    EvidenceLedger,
    InvestigationStep,
    OutcomeRevision,
    ToolCall,
)
from backend.persistence.repositories import IncidentRepository

logger = logging.getLogger("opsblaze.workbench")


class WorkbenchService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._inc_repo = IncidentRepository(session_factory)

    async def get_workbench_snapshot(
        self, tenant_id: uuid.UUID, incident_id: uuid.UUID
    ) -> tuple[WorkbenchSnapshotResponse, str]:
        """Fetch full live workbench state snapshot and calculate deterministic ETag."""
        incident = await self._inc_repo.get_incident(tenant_id, incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)

        async with self._session_factory() as session:
            # 1. Fetch Investigation Steps
            steps_rows = (
                await session.execute(
                    select(InvestigationStep)
                    .where(
                        InvestigationStep.tenant_id == tenant_id,
                        InvestigationStep.incident_id == incident_id,
                    )
                    .order_by(InvestigationStep.step_no.asc())
                )
            ).scalars().all()

            # 2. Fetch Evidence Ledger records
            evidence_rows = (
                await session.execute(
                    select(EvidenceLedger)
                    .where(
                        EvidenceLedger.tenant_id == tenant_id,
                        EvidenceLedger.incident_id == incident_id,
                    )
                    .order_by(EvidenceLedger.captured_at.asc())
                )
            ).scalars().all()

            # 3. Fetch Tool Calls
            tool_rows = (
                await session.execute(
                    select(ToolCall)
                    .where(
                        ToolCall.tenant_id == tenant_id,
                        ToolCall.incident_id == incident_id,
                    )
                    .order_by(ToolCall.step_no.asc(), ToolCall.created_at.asc())
                )
            ).scalars().all()

            # 4. Fetch Budget Ledger
            budget_row = (
                await session.execute(
                    select(BudgetLedger).where(
                        BudgetLedger.tenant_id == tenant_id,
                        BudgetLedger.incident_id == incident_id,
                    )
                )
            ).scalar_one_or_none()

            # 5. Fetch Active Approval Request
            approval_row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.incident_id == incident_id,
                        ApprovalRequest.state.in_(
                            [
                                ApprovalState.PENDING.value,
                                ApprovalState.APPROVED.value,
                                ApprovalState.DISPATCHING.value,
                            ]
                        ),
                    )
                    .order_by(ApprovalRequest.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            # 6. Fetch Latest Outcome Revision
            outcome_row = (
                await session.execute(
                    select(OutcomeRevision)
                    .where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.incident_id == incident_id,
                    )
                    .order_by(OutcomeRevision.revision_no.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        # Format Steps
        step_items: list[InvestigationStepItem] = [
            InvestigationStepItem(
                id=s.id,
                step_no=s.step_no,
                loop_state=s.loop_state,
                prompt_digest=s.prompt_digest,
                token_count=s.token_count,
                abstention_reason=s.abstention_reason,
                created_at=s.created_at,
            )
            for s in steps_rows
        ]

        # Format Evidence
        evidence_items: list[EvidenceItem] = [
            EvidenceItem(
                id=e.id,
                tool_call_id=e.tool_call_id,
                source=e.source,
                source_version=e.source_version,
                target_id=e.target_id,
                target_version=e.target_version,
                observed_at=e.observed_at,
                captured_at=e.captured_at,
                sha256_digest=e.sha256_digest,
                raw_payload_redacted=e.raw_payload_redacted,
                classification=e.classification,
                validity_status=e.validity_status,
            )
            for e in evidence_rows
        ]

        # Format Tool Calls
        tool_items: list[ToolCallItem] = [
            ToolCallItem(
                id=t.id,
                step_no=t.step_no,
                tool_id=t.tool_id,
                tool_version=t.tool_version,
                target_id=t.target_id,
                status=t.status,
                duration_ms=t.duration_ms,
                args_digest=t.args_digest,
                output_digest=t.output_digest,
                error_detail=t.error_detail,
                created_at=t.created_at,
            )
            for t in tool_rows
        ]

        # Format Hypotheses from latest step
        hypotheses: list[HypothesisItem] = []
        abstention = AbstentionDetail(abstained=False)

        if steps_rows:
            latest_step = steps_rows[-1]
            raw_hyp = latest_step.hypotheses_json
            if isinstance(raw_hyp, dict):
                # Check if it has a list of hypotheses
                hyp_list = raw_hyp.get("hypotheses", [])
                if isinstance(hyp_list, list):
                    for idx, h in enumerate(hyp_list, start=1):
                        if isinstance(h, dict):
                            hypotheses.append(
                                HypothesisItem(
                                    hypothesis_id=str(h.get("id") or f"hyp-{idx}"),
                                    title=str(h.get("title") or h.get("name") or "Hypothesis"),
                                    confidence=float(h.get("confidence") or 0.5),
                                    status=str(h.get("status") or "ACTIVE"),
                                    rationale=str(h.get("rationale") or h.get("summary") or ""),
                                    rank=int(h.get("rank") or idx),
                                )
                            )

            if latest_step.abstention_reason or incident.status == IncidentStatus.NEEDS_REVIEW:
                abstention = AbstentionDetail(
                    abstained=True,
                    reason=latest_step.abstention_reason
                    or "Incident escalated to human intervention (NEEDS_REVIEW)",
                    confidence=0.0,
                    missing_evidence_types=[],
                )

        # Format Budget
        budget = (
            BudgetHeadroom(
                reserved_tokens=budget_row.reserved_tokens,
                settled_tokens=budget_row.settled_tokens,
                reserved_probes=budget_row.reserved_probes,
                settled_probes=budget_row.settled_probes,
                reserved_cost_micros=budget_row.reserved_cost_micros,
                settled_cost_micros=budget_row.settled_cost_micros,
                max_steps=10,
                current_step=len(step_items),
            )
            if budget_row
            else BudgetHeadroom(max_steps=10, current_step=len(step_items))
        )

        # Format Active Approval
        active_approval: ApprovalResponse | None = (
            approval_to_response(approval_row) if approval_row else None
        )

        approval_state_tag = active_approval.state.value if active_approval else "none"
        settled_tokens = budget_row.settled_tokens if budget_row else 0
        settled_cost = budget_row.settled_cost_micros if budget_row else 0
        outcome_label_val = outcome_row.label if outcome_row else None
        outcome_rev_val = outcome_row.revision_no if outcome_row else None
        outcome_disputed_val = (outcome_label_val == "DISPUTED") if outcome_label_val else False

        etag = (
            f'"{incident.status_version}:{len(step_items)}:{len(evidence_items)}:'
            f'{len(tool_items)}:{settled_tokens}:{settled_cost}:{approval_state_tag}:'
            f'{outcome_label_val or "none"}:{outcome_rev_val or 0}"'
        )

        response = WorkbenchSnapshotResponse(
            incident=incident,
            hypotheses=hypotheses,
            evidence=evidence_items,
            steps=step_items,
            tool_calls=tool_items,
            budget=budget,
            active_approval=active_approval,
            abstention=abstention,
            projection_lag_ms=incident.projection_lag_ms,
            etag=etag,
            outcome_label=outcome_label_val,
            outcome_revision_no=outcome_rev_val,
            outcome_is_disputed=outcome_disputed_val,
        )
        return response, etag
