"""Service for OpsBlaze Feature P3 — Evidence-to-Effect Trace Explorer."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalState
from backend.domain.incident import IncidentNotFoundError
from backend.domain.trace import (
    TraceEdge,
    TraceGraphResponse,
    TraceNode,
    TraceNodeKind,
    TraceRelation,
)
from backend.observability.metrics import redact_value
from backend.persistence.models import (
    ApprovalRequest,
    CandidateEvaluationManifest,
    CandidateEvaluationReport,
    EffectReceipt,
    EvidenceLedger,
    ExperienceCapsule,
    FailureCase,
    FailureCluster,
    ImprovementProposal,
    InvestigationStep,
    LessonUsage,
    OperationalLesson,
    OutcomeRevision,
    ToolCall,
)
from backend.persistence.repositories import IncidentRepository

logger = logging.getLogger("opsblaze.trace")


class TraceService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._inc_repo = IncidentRepository(session_factory)

    async def get_incident_trace_graph(
        self, tenant_id: uuid.UUID, incident_id: uuid.UUID
    ) -> TraceGraphResponse:
        """Project authoritative PostgreSQL records into a causally linked Directed Acyclic Graph (DAG)."""
        incident = await self._inc_repo.get_incident(tenant_id, incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)

        async with self._session_factory() as session:
            # 1. Fetch Steps
            steps = (
                await session.execute(
                    select(InvestigationStep)
                    .where(
                        InvestigationStep.tenant_id == tenant_id,
                        InvestigationStep.incident_id == incident_id,
                    )
                    .order_by(InvestigationStep.step_no.asc())
                )
            ).scalars().all()

            # 2. Fetch Tool Calls
            tool_calls = (
                await session.execute(
                    select(ToolCall)
                    .where(
                        ToolCall.tenant_id == tenant_id,
                        ToolCall.incident_id == incident_id,
                    )
                    .order_by(ToolCall.step_no.asc(), ToolCall.created_at.asc())
                )
            ).scalars().all()

            # 3. Fetch Evidence
            evidence = (
                await session.execute(
                    select(EvidenceLedger)
                    .where(
                        EvidenceLedger.tenant_id == tenant_id,
                        EvidenceLedger.incident_id == incident_id,
                    )
                    .order_by(EvidenceLedger.captured_at.asc())
                )
            ).scalars().all()

            # 4. Fetch Approvals
            approvals = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.incident_id == incident_id,
                    )
                    .order_by(ApprovalRequest.created_at.asc())
                )
            ).scalars().all()

            # 5. Fetch Effect Receipts (scoped to this tenant and incident)
            receipts = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.tenant_id == tenant_id,
                        EffectReceipt.incident_id == incident_id,
                    )
                    .order_by(EffectReceipt.created_at.asc())
                )
            ).scalars().all()

            # 6. Fetch Outcome Revisions
            outcomes = (
                await session.execute(
                    select(OutcomeRevision)
                    .where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.incident_id == incident_id,
                    )
                    .order_by(OutcomeRevision.revision_no.asc())
                )
            ).scalars().all()

            # 7. Fetch Experience Capsules
            experiences = (
                await session.execute(
                    select(ExperienceCapsule)
                    .where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.incident_id == incident_id,
                    )
                    .order_by(ExperienceCapsule.created_at.asc())
                )
            ).scalars().all()

            # 8. Fetch Failure Cases
            failure_cases = (
                await session.execute(
                    select(FailureCase)
                    .where(
                        FailureCase.tenant_id == tenant_id,
                        FailureCase.incident_id == incident_id,
                    )
                    .order_by(FailureCase.created_at.asc())
                )
            ).scalars().all()

            # 9. Fetch Lesson Usages for this incident
            lesson_usages = (
                await session.execute(
                    select(LessonUsage)
                    .where(
                        LessonUsage.tenant_id == tenant_id,
                        LessonUsage.incident_id == incident_id,
                    )
                    .order_by(LessonUsage.used_at.asc())
                )
            ).scalars().all()

            # Fetch relevant clusters and proposals
            cluster_ids = {fc.cluster_id for fc in failure_cases if fc.cluster_id}
            clusters_by_id: dict[uuid.UUID, FailureCluster] = {}
            proposals_by_id: dict[uuid.UUID, ImprovementProposal] = {}
            if cluster_ids:
                cl_rows = (
                    await session.execute(
                        select(FailureCluster).where(
                            FailureCluster.tenant_id == tenant_id,
                            FailureCluster.id.in_(cluster_ids),
                        )
                    )
                ).scalars().all()
                clusters_by_id = {c.id: c for c in cl_rows}

                prop_rows = (
                    await session.execute(
                        select(ImprovementProposal).where(
                            ImprovementProposal.tenant_id == tenant_id,
                            ImprovementProposal.cluster_id.in_(cluster_ids),
                        )
                    )
                ).scalars().all()
                proposals_by_id = {p.id: p for p in prop_rows}

            eval_manifests: list[CandidateEvaluationManifest] = []
            eval_reports_by_manifest: dict[uuid.UUID, CandidateEvaluationReport] = {}
            if proposals_by_id:
                manifest_rows = (
                    await session.execute(
                        select(CandidateEvaluationManifest).where(
                            CandidateEvaluationManifest.tenant_id == tenant_id,
                            CandidateEvaluationManifest.source_proposal_id.in_(proposals_by_id.keys()),
                        )
                    )
                ).scalars().all()
                eval_manifests = list(manifest_rows)
                if eval_manifests:
                    manifest_ids = [m.id for m in eval_manifests]
                    rep_rows = (
                        await session.execute(
                            select(CandidateEvaluationReport).where(
                                CandidateEvaluationReport.tenant_id == tenant_id,
                                CandidateEvaluationReport.manifest_id.in_(manifest_ids),
                            )
                        )
                    ).scalars().all()
                    for rep in rep_rows:
                        eval_reports_by_manifest[rep.manifest_id] = rep

            # Fetch lessons cited in usages
            lesson_keys = {(u.lesson_id, u.lesson_version) for u in lesson_usages}
            lessons_by_key: dict[tuple[uuid.UUID, int], OperationalLesson] = {}
            if lesson_keys:
                lesson_ids = {k[0] for k in lesson_keys}
                l_rows = (
                    await session.execute(
                        select(OperationalLesson).where(
                            OperationalLesson.tenant_id == tenant_id,
                            OperationalLesson.lesson_id.in_(lesson_ids),
                        )
                    )
                ).scalars().all()
                lessons_by_key = {(l.lesson_id, l.version): l for l in l_rows}

        nodes: list[TraceNode] = []
        edges: list[TraceEdge] = []
        missing_links: list[str] = []
        node_ids: set[str] = set()

        root_id = str(incident.id)

        # Root Incident Node
        inc_node = TraceNode(
            id=root_id,
            kind=TraceNodeKind.INCIDENT,
            label=f"Incident ({incident.status.value})",
            timestamp=incident.created_at,
            status=incident.status.value,
            metadata=redact_value(
                {
                    "client_key": incident.client_key,
                    "target_id": incident.target_id,
                    "status_version": incident.status_version,
                    "workflow_id": incident.workflow_id,
                }
            ),
        )
        nodes.append(inc_node)
        node_ids.add(root_id)

        # Step mapping
        step_id_map: dict[int, InvestigationStep] = {}
        prev_step_id: str | None = None

        for s in steps:
            s_id = str(s.id)
            step_id_map[s.step_no] = s
            s_node = TraceNode(
                id=s_id,
                kind=TraceNodeKind.STEP,
                label=f"Step #{s.step_no} ({s.loop_state})",
                timestamp=s.created_at,
                status=s.loop_state,
                metadata=redact_value(
                    {
                        "step_no": s.step_no,
                        "token_count": s.token_count,
                        "abstention_reason": s.abstention_reason,
                    }
                ),
            )
            nodes.append(s_node)
            node_ids.add(s_id)

            if prev_step_id is None:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=s_id,
                        relation=TraceRelation.INITIATED,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=prev_step_id,
                        target=s_id,
                        relation=TraceRelation.NEXT_STEP,
                    )
                )
            prev_step_id = s_id

            # If step proposed a plan
            if s.proposed_plan_json:
                plan_id = f"plan:{s.id}"
                plan_title = str(s.proposed_plan_json.get("plan_id") or f"plan-{s.step_no}")
                plan_node = TraceNode(
                    id=plan_id,
                    kind=TraceNodeKind.PLAN,
                    label=f"Plan ({plan_title})",
                    timestamp=s.created_at,
                    status="PROPOSED",
                    metadata=redact_value(s.proposed_plan_json),
                )
                nodes.append(plan_node)
                node_ids.add(plan_id)
                edges.append(
                    TraceEdge(
                        source=s_id,
                        target=plan_id,
                        relation=TraceRelation.PROPOSED,
                    )
                )

        # Tool Calls
        for tc in tool_calls:
            tc_id = str(tc.id)
            tc_node = TraceNode(
                id=tc_id,
                kind=TraceNodeKind.TOOL_CALL,
                label=f"{tc.tool_id} -> {tc.target_id}",
                timestamp=tc.created_at,
                status=tc.status,
                metadata=redact_value(
                    {
                        "tool_id": tc.tool_id,
                        "tool_version": tc.tool_version,
                        "duration_ms": tc.duration_ms,
                        "status": tc.status,
                    }
                ),
            )
            nodes.append(tc_node)
            node_ids.add(tc_id)

            parent_step = step_id_map.get(tc.step_no)
            if parent_step:
                edges.append(
                    TraceEdge(
                        source=str(parent_step.id),
                        target=tc_id,
                        relation=TraceRelation.INVOKED,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=tc_id,
                        relation=TraceRelation.INVOKED,
                    )
                )
                missing_links.append(f"ToolCall {tc.id} step_no={tc.step_no} has no matching step")

        # Evidence
        ev_id_set: set[str] = set()
        for ev in evidence:
            ev_id = str(ev.id)
            ev_id_set.add(ev_id)
            ev_node = TraceNode(
                id=ev_id,
                kind=TraceNodeKind.EVIDENCE,
                label=f"Evidence ({ev.source}:{ev.classification})",
                timestamp=ev.captured_at,
                status=ev.validity_status,
                metadata=redact_value(
                    {
                        "source": ev.source,
                        "digest": ev.sha256_digest,
                        "payload": ev.raw_payload_redacted,
                    }
                ),
            )
            nodes.append(ev_node)
            node_ids.add(ev_id)

            if ev.tool_call_id and str(ev.tool_call_id) in node_ids:
                edges.append(
                    TraceEdge(
                        source=str(ev.tool_call_id),
                        target=ev_id,
                        relation=TraceRelation.PRODUCED,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=ev_id,
                        relation=TraceRelation.PRODUCED,
                    )
                )
                if ev.tool_call_id:
                    missing_links.append(
                        f"Evidence {ev.id} references missing tool call {ev.tool_call_id}"
                    )

        # Connect evidence used in steps
        for s in steps:
            if s.evidence_ids_used and isinstance(s.evidence_ids_used, list):
                for used_ev in s.evidence_ids_used:
                    used_str = str(used_ev)
                    if used_str in ev_id_set:
                        edges.append(
                            TraceEdge(
                                source=used_str,
                                target=str(s.id),
                                relation=TraceRelation.USED_IN,
                            )
                        )

        # Approvals
        app_id_set: set[str] = set()
        for app in approvals:
            app_id = str(app.id)
            app_id_set.add(app_id)
            app_node = TraceNode(
                id=app_id,
                kind=TraceNodeKind.APPROVAL,
                label=f"Approval ({app.state})",
                timestamp=app.created_at,
                status=app.state,
                metadata=redact_value(
                    {
                        "plan_id": app.plan_id,
                        "reviewer_id": app.reviewer_id,
                        "reviewer_role": app.reviewer_role,
                        "expected_resource_version": app.expected_resource_version,
                    }
                ),
            )
            nodes.append(app_node)
            node_ids.add(app_id)

            # Link approval to plan node or step
            plan_node_id = f"plan:{app.step_id}" if app.step_id else None
            if plan_node_id and plan_node_id in node_ids:
                edges.append(
                    TraceEdge(
                        source=plan_node_id,
                        target=app_id,
                        relation=TraceRelation.REQUESTED_APPROVAL,
                    )
                )
            elif app.step_id and str(app.step_id) in node_ids:
                edges.append(
                    TraceEdge(
                        source=str(app.step_id),
                        target=app_id,
                        relation=TraceRelation.REQUESTED_APPROVAL,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=app_id,
                        relation=TraceRelation.REQUESTED_APPROVAL,
                    )
                )

        # Effect Receipts
        for rec in receipts:
            rec_id = str(rec.id)
            warning_msg = None
            # Find matching approval by plan_digest and action_digest with valid execution state
            matching_app = next(
                (
                    app
                    for app in approvals
                    if app.canonical_plan_digest == rec.plan_digest
                    and app.action_digest == rec.action_digest
                    and app.state
                    in (
                        ApprovalState.CONSUMED.value,
                        ApprovalState.DISPATCHING.value,
                        ApprovalState.APPROVED.value,
                    )
                ),
                None,
            )
            matching_app_id = str(matching_app.id) if matching_app else None
            if not matching_app_id:
                warning_msg = "Unapproved external mutation: no corresponding approval request"
                missing_links.append(
                    f"Receipt {rec.id} has no matching approval request in audit DAG"
                )

            rec_node = TraceNode(
                id=rec_id,
                kind=TraceNodeKind.EFFECT_RECEIPT,
                label=f"Receipt ({rec.state})",
                timestamp=rec.created_at,
                status=rec.state,
                warning=warning_msg,
                metadata=redact_value(
                    {
                        "target_id": rec.target_id,
                        "fence": rec.fence,
                        "plan_digest": rec.plan_digest,
                        "action_digest": rec.action_digest,
                    }
                ),
            )
            nodes.append(rec_node)
            node_ids.add(rec_id)

            if matching_app_id:
                edges.append(
                    TraceEdge(
                        source=matching_app_id,
                        target=rec_id,
                        relation=TraceRelation.AUTHORIZED_EFFECT,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=rec_id,
                        relation=TraceRelation.AUTHORIZED_EFFECT,
                    )
                )

        # 6. Outcome Nodes and Edges
        last_outcome_id: str | None = None
        for rev in outcomes:
            rev_node_id = str(rev.id)
            rev_node = TraceNode(
                id=rev_node_id,
                kind=TraceNodeKind.OUTCOME,
                label=f"Outcome Rev {rev.revision_no} ({rev.label})",
                timestamp=rev.decided_at,
                status=rev.label,
                metadata=redact_value(
                    {
                        "revision_no": rev.revision_no,
                        "confidence_band": rev.confidence_band,
                        "dispute_reason": rev.dispute_reason,
                        "reviewer_id": rev.reviewer_id,
                    }
                ),
            )
            nodes.append(rev_node)
            node_ids.add(rev_node_id)
            last_outcome_id = rev_node_id

            # Causal edge: if prior revision exists, connect prior -> this; else connect from receipt/root
            if rev.prior_revision_id and str(rev.prior_revision_id) in node_ids:
                edges.append(
                    TraceEdge(
                        source=str(rev.prior_revision_id),
                        target=rev_node_id,
                        relation=TraceRelation.ADJUDICATED,
                    )
                )
            elif receipts:
                edges.append(
                    TraceEdge(
                        source=str(receipts[-1].id),
                        target=rev_node_id,
                        relation=TraceRelation.ADJUDICATED,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=rev_node_id,
                        relation=TraceRelation.ADJUDICATED,
                    )
                )

        # 7. Experience Capsule Nodes and Edges
        for exp in experiences:
            exp_node_id = str(exp.id)
            exp_node = TraceNode(
                id=exp_node_id,
                kind=TraceNodeKind.EXPERIENCE,
                label=f"Experience ({exp.lifecycle_state})",
                timestamp=exp.created_at,
                status=exp.lifecycle_state,
                metadata=redact_value(
                    {
                        "classification": exp.classification,
                        "consent_epoch": exp.consent_epoch,
                        "model_id": exp.model_id,
                        "schema_version": exp.schema_version,
                    }
                ),
            )
            nodes.append(exp_node)
            node_ids.add(exp_node_id)

            # Causal edge: connect from outcome if exists, or last step, or root
            if last_outcome_id and last_outcome_id in node_ids:
                edges.append(
                    TraceEdge(
                        source=last_outcome_id,
                        target=exp_node_id,
                        relation=TraceRelation.CAPTURED_IN,
                    )
                )
            elif prev_step_id and prev_step_id in node_ids:
                edges.append(
                    TraceEdge(
                        source=prev_step_id,
                        target=exp_node_id,
                        relation=TraceRelation.CAPTURED_IN,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=exp_node_id,
                        relation=TraceRelation.CAPTURED_IN,
                    )
                )

        # 8. Failure Cases and Clusters
        for fc in failure_cases:
            fc_id = str(fc.id)
            fc_node = TraceNode(
                id=fc_id,
                kind=TraceNodeKind.FAILURE_CASE,
                label=f"Failure Case ({fc.failure_origin_code})",
                timestamp=fc.created_at,
                status=fc.classification_status,
                metadata=redact_value(
                    {
                        "origin_code": fc.failure_origin_code,
                        "category": fc.first_divergent_step_category,
                        "subsystem_version": fc.subsystem_version,
                        "digest": fc.evidence_digest,
                    }
                ),
            )
            nodes.append(fc_node)
            node_ids.add(fc_id)

            # Causal link from outcome revision
            outcome_ref_id = str(fc.outcome_revision_id)
            if outcome_ref_id in node_ids:
                edges.append(
                    TraceEdge(
                        source=outcome_ref_id,
                        target=fc_id,
                        relation=TraceRelation.CLASSIFIED_AS,
                    )
                )
            else:
                edges.append(
                    TraceEdge(
                        source=root_id,
                        target=fc_id,
                        relation=TraceRelation.CLASSIFIED_AS,
                    )
                )

            # Link failure case to its cluster if present
            if fc.cluster_id and fc.cluster_id in clusters_by_id:
                cl = clusters_by_id[fc.cluster_id]
                cl_id = str(cl.id)
                if cl_id not in node_ids:
                    cl_node = TraceNode(
                        id=cl_id,
                        kind=TraceNodeKind.FAILURE_CLUSTER,
                        label=f"Cluster ({cl.failure_origin_code} - {cl.case_count} cases)",
                        timestamp=cl.created_at,
                        status=cl.cluster_status,
                        metadata=redact_value(
                            {
                                "origin_code": cl.failure_origin_code,
                                "category": cl.first_divergent_step_category,
                                "case_count": cl.case_count,
                                "version": cl.version,
                            }
                        ),
                    )
                    nodes.append(cl_node)
                    node_ids.add(cl_id)

                edges.append(
                    TraceEdge(
                        source=fc_id,
                        target=cl_id,
                        relation=TraceRelation.CLUSTERED_IN,
                    )
                )

                # Link proposals for this cluster
                for prop in proposals_by_id.values():
                    if prop.cluster_id == cl.id:
                        prop_id = str(prop.id)
                        if prop_id not in node_ids:
                            prop_node = TraceNode(
                                id=prop_id,
                                kind=TraceNodeKind.PROPOSAL,
                                label=f"Proposal ({prop.proposal_type})",
                                timestamp=prop.created_at,
                                status=prop.status,
                                metadata=redact_value(
                                    {
                                        "proposal_type": prop.proposal_type,
                                        "target_component": prop.target_component,
                                        "version": prop.version,
                                        "decision": prop.review_decision,
                                    }
                                ),
                            )
                            nodes.append(prop_node)
                            node_ids.add(prop_id)

                        edges.append(
                            TraceEdge(
                                source=cl_id,
                                target=prop_id,
                                relation=TraceRelation.PROPOSED_FROM,
                            )
                        )

                        # Link evaluation manifests for this proposal
                        for man in eval_manifests:
                            if man.source_proposal_id == prop.id:
                                man_id = str(man.id)
                                if man_id not in node_ids:
                                    man_node = TraceNode(
                                        id=man_id,
                                        kind=TraceNodeKind.EVALUATION_MANIFEST,
                                        label=f"Evaluation ({man.lifecycle_state})",
                                        timestamp=man.created_at,
                                        status=man.lifecycle_state,
                                        metadata=redact_value(
                                            {
                                                "title": man.title,
                                                "provenance": man.candidate_provenance_type,
                                                "sample_size": man.sample_size_plan,
                                                "sha256": man.manifest_sha256[:16],
                                            }
                                        ),
                                    )
                                    nodes.append(man_node)
                                    node_ids.add(man_id)

                                edges.append(
                                    TraceEdge(
                                        source=prop_id,
                                        target=man_id,
                                        relation=TraceRelation.EVALUATED_IN,
                                    )
                                )

                                # Link evaluation report if present
                                eval_rep = eval_reports_by_manifest.get(man.id)
                                if eval_rep:
                                    rep_id = str(eval_rep.id)
                                    if rep_id not in node_ids:
                                        rep_node = TraceNode(
                                            id=rep_id,
                                            kind=TraceNodeKind.EVALUATION_REPORT,
                                            label=f"Report ({eval_rep.report_status})",
                                            timestamp=eval_rep.evaluated_at,
                                            status=eval_rep.report_status,
                                            metadata=redact_value(
                                                {
                                                    "recovery_delta": eval_rep.recovery_delta,
                                                    "promotion_eligible": eval_rep.promotion_eligible,
                                                    "decision": eval_rep.review_decision,
                                                }
                                            ),
                                        )
                                        nodes.append(rep_node)
                                        node_ids.add(rep_id)

                                    edges.append(
                                        TraceEdge(
                                            source=man_id,
                                            target=rep_id,
                                            relation=TraceRelation.REPORTED_BY,
                                        )
                                    )

        # 9. Cited Operational Lessons
        for usage in lesson_usages:
            lesson_key = (usage.lesson_id, usage.lesson_version)
            lesson = lessons_by_key.get(lesson_key)
            if lesson:
                l_node_id = str(lesson.id)
                if l_node_id not in node_ids:
                    l_node = TraceNode(
                        id=l_node_id,
                        kind=TraceNodeKind.LESSON,
                        label=f"Lesson ({lesson.title[:30]})",
                        timestamp=lesson.created_at,
                        status=lesson.lifecycle_state,
                        metadata=redact_value(
                            {
                                "lesson_type": lesson.lesson_type,
                                "confidence": lesson.confidence_band,
                                "version": lesson.version,
                            }
                        ),
                    )
                    nodes.append(l_node)
                    node_ids.add(l_node_id)

                # Source link: from step if step_id exists, else from root
                src_step_id = str(usage.step_id) if usage.step_id else None
                source_node = src_step_id if src_step_id and src_step_id in node_ids else root_id
                edges.append(
                    TraceEdge(
                        source=source_node,
                        target=l_node_id,
                        relation=TraceRelation.CITED_LESSON,
                    )
                )

        # Cycle detection via Kahn's algorithm
        has_cycles = _detect_cycles(nodes, edges)

        return TraceGraphResponse(
            incident_id=incident_id,
            tenant_id=tenant_id,
            root_node_id=root_id,
            nodes=nodes,
            edges=edges,
            missing_links=missing_links,
            has_cycles=has_cycles,
        )


def _detect_cycles(nodes: list[TraceNode], edges: list[TraceEdge]) -> bool:
    """Validate acyclicity of the projected graph. Returns True if cycle detected."""
    adj: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {n.id: 0 for n in nodes}

    for e in edges:
        adj[e.source].append(e.target)
        in_degree[e.target] = in_degree.get(e.target, 0) + 1

    queue = deque([node_id for node_id, deg in in_degree.items() if deg == 0])
    visited_count = 0

    while queue:
        curr = queue.popleft()
        visited_count += 1
        for neighbor in adj[curr]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If visited count != total nodes, there is a cycle
    return visited_count != len(nodes)
