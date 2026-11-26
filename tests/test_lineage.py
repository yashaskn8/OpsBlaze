
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.investigator.baseline import inspect
from backend.lineage import build_lineage, read_lineage_page
from backend.models import HypothesisStep, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_tenant_scoped_lineage_root(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "root", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert graph["nodes"][0]["id"] == f"incident:{incident.id}"
        assert graph["trace_spans_available"] is False
        assert await build_lineage(session, "other", incident.id) is None


@pytest.mark.asyncio
async def test_evidence_to_plan_links_are_persistently_reconstructed(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "link", IncidentCreate(target_id="checkout")
        )
    assert await inspect(sessions_fixture, "t", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert len([n for n in graph["nodes"] if n["kind"] == "evidence"]) == 2
        assert len([n for n in graph["nodes"] if n["kind"] == "plan"]) == 1
        assert len([e for e in graph["edges"] if e["relation"] == "JUSTIFIED_BY"]) == 2


@pytest.mark.asyncio
async def test_missing_evidence_reference_is_reported_not_invented(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "broken", IncidentCreate(target_id="checkout")
        )
    await inspect(sessions_fixture, "t", incident.id)
    phantom = str(uuid4())
    async with sessions_fixture() as session:
        async with session.begin():
            step = await session.scalar(
                select(HypothesisStep).where(
                    HypothesisStep.incident_id == incident.id,
                    HypothesisStep.step_no == 1,
                )
            )
            step.evidence_ids = [phantom]
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert any(m["missing_target"] == "evidence:unavailable" for m in graph["missing_refs"])
        assert phantom not in str(graph["missing_refs"])
        assert not any(
            n["id"] == f"evidence:{phantom}" for n in graph["nodes"]
        )


@pytest.mark.asyncio
async def test_redteam_large_lineage_never_claims_complete_when_truncated(sessions_fixture):
    """P3 must report omitted records instead of disguising a partial graph."""
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "many-steps",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                HypothesisStep(
                    id=uuid4(), incident_id=incident.id, step_no=i,
                    model_id="deterministic-baseline-v1", prompt_version="none",
                    evidence_ids=[], cause_scores={}, probe_id="simulator.health",
                    decision="ABSTAINED", token_reserved=0, token_used=0,
                )
                for i in range(1, 52)
            ])
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "tenant", incident.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert graph["truncated_sources"] == ["hypothesis"]
        assert len([n for n in graph["nodes"] if n["kind"] == "hypothesis"]) == 50
        assert await build_lineage(session, "foreign", incident.id) is None

        first = await read_lineage_page(
            session, "tenant", incident.id, "hypothesis", limit=50,
        )
        assert len(first["nodes"]) == 50 and first["next_cursor"]
        second = await read_lineage_page(
            session, "tenant", incident.id, "hypothesis",
            cursor=__import__("uuid").UUID(first["next_cursor"]), limit=50,
        )
        assert len(second["nodes"]) == 1 and second["next_cursor"] is None
        assert not {n["id"] for n in first["nodes"]}.intersection(
            n["id"] for n in second["nodes"]
        )
        assert await read_lineage_page(
            session, "foreign", incident.id, "hypothesis",
        ) is None


@pytest.mark.asyncio
async def test_redteam_approval_grant_is_part_of_effect_causal_chain(sessions_fixture):
    from datetime import datetime, timedelta, timezone

    from backend.models import ApprovalGrant, EffectReceipt, IncidentPlan

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t", target_id="checkout", version=1,
                healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "causal-approval",
            IncidentCreate(target_id="checkout"),
        )
    assert await inspect(sessions_fixture, "t", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        async with session.begin():
            plan = await session.scalar(select(IncidentPlan))
            plan.status = "APPROVED"
            plan.version = 2
            plan.decided_by = "DEV_REVIEWER_CREDENTIAL"
            plan_id, digest = plan.id, plan.plan_digest

    grant_id, effect_id = uuid4(), uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(ApprovalGrant(
                id=grant_id, tenant_id="t", plan_id=plan_id,
                plan_digest=digest, reviewer_id="DEV_REVIEWER_CREDENTIAL",
                scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                state="CONSUMED", reason="authorized synthetic trace",
                version=2,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ))
            session.add(EffectReceipt(
                id=effect_id, tenant_id="t", incident_id=incident.id,
                plan_digest=digest, target_id="checkout",
                adapter_id="simulator", idempotency_key=str(effect_id),
                fence=1, expected_version=1, state="SENT",
                attempt_count=1,
            ))
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        page = await read_lineage_page(session, "t", incident.id, "effect")
    assert {
        edge["relation"] for edge in page["edges"]
    } >= {"BELONGS_TO", "AUTHORIZED_BY", "AUTHORIZED_BY_GRANT"}
    assert page["missing_refs"] == []
    assert page["links_truncated"] is False
    assert graph["projection"] == "COMPLETE"
    assert any(n["id"] == f"approval:{grant_id}" for n in graph["nodes"])
    assert {
        (e["from"], e["to"], e["relation"]) for e in graph["edges"]
    } >= {
        (f"approval:{grant_id}", f"plan:{plan_id}", "REVIEWS"),
        (f"effect:{effect_id}", f"approval:{grant_id}", "AUTHORIZED_BY_GRANT"),
    }
    async with sessions_fixture() as session:
        assert await build_lineage(session, "wrong-tenant", incident.id) is None

    # A durable grant remains visible in historical provenance, but a
    # rejected or digest-mismatched grant must NEVER be marked as authority.
    for field, value in (
        ("state", "REJECTED"),
        ("state", "APPROVED"),
        ("plan_digest", "f" * 64),
        ("reviewer_id", "WRONG_REVIEWER"),
    ):
        async with sessions_fixture() as session:
            async with session.begin():
                grant = await session.get(ApprovalGrant, grant_id)
                grant.state = "CONSUMED"
                grant.plan_digest = digest
                setattr(grant, field, value)
        async with sessions_fixture() as session:
            broken = await build_lineage(session, "t", incident.id)
        assert broken["projection"] == "PARTIAL_LINKS"
        assert any(
            m["source"] == f"effect:{effect_id}"
            and m["relation"] == "AUTHORIZED_BY_GRANT"
            for m in broken["missing_refs"]
        )
        assert not any(
            e["from"] == f"effect:{effect_id}"
            and e["relation"] == "AUTHORIZED_BY_GRANT"
            for e in broken["edges"]
        )

    # Merely matching a plan digest is not evidence of dispatch authority.
    for field, value in (
        ("target_id", "another-target"),
        ("expected_version", 99),
        ("adapter_id", "unqualified-external"),
        ("attempt_count", 0),
        ("state", "PREPARED"),
    ):
        async with sessions_fixture() as session:
            async with session.begin():
                grant = await session.get(ApprovalGrant, grant_id)
                grant.state = "CONSUMED"
                grant.plan_digest = digest
                grant.reviewer_id = "DEV_REVIEWER_CREDENTIAL"
                receipt = await session.get(EffectReceipt, effect_id)
                receipt.target_id = "checkout"
                receipt.expected_version = 1
                receipt.adapter_id = "simulator"
                receipt.attempt_count = 1
                receipt.state = "SENT"
                setattr(receipt, field, value)
                if field == "state":
                    # A valid PREPARED receipt has no durable send attempt.
                    receipt.attempt_count = 0
        async with sessions_fixture() as session:
            graph = await build_lineage(session, "t", incident.id)
            page = await read_lineage_page(session, "t", incident.id, "effect")
        assert not any(
            edge["relation"] in {"AUTHORIZED_BY", "AUTHORIZED_BY_GRANT"}
            for edge in page["edges"]
        )
        effect_edges = [
            edge["relation"] for edge in graph["edges"]
            if edge["from"] == f"effect:{effect_id}"
        ]
        assert "AUTHORIZED_BY" not in effect_edges
        assert "AUTHORIZED_BY_GRANT" not in effect_edges
        if field == "state":
            assert "PREPARED_FOR" in effect_edges
        else:
            assert graph["projection"] == "PARTIAL_LINKS"
            assert any(
                item["source"] == f"effect:{effect_id}"
                for item in graph["missing_refs"]
            )



@pytest.mark.asyncio
async def test_redteam_paginated_lineage_resolves_real_references_without_leaks(
    sessions_fixture,
):
    from datetime import datetime, timezone
    from backend.models import Evidence

    async with sessions_fixture() as session:
        own = await admit_incident(
            session, "alpha", "page-refs-own",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        foreign = await admit_incident(
            session, "beta", "page-refs-other",
            IncidentCreate(target_id="sensitive-service"),
        )
    own_evidence_id, foreign_evidence_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    async with sessions_fixture() as session:
        async with session.begin():
            for tenant, incident_id, evidence_id, target in [
                ("alpha", own.id, own_evidence_id, "checkout"),
                ("beta", foreign.id, foreign_evidence_id, "sensitive-service"),
            ]:
                session.add(Evidence(
                    id=evidence_id, tenant_id=tenant, incident_id=incident_id,
                    target_id=target, source="simulator.health",
                    source_version="simulator-v1", target_version=1,
                    observed_at=now, sha256="e" * 64,
                    redacted_excerpt='{"healthy":false}',
                    classification="INTERNAL", validity="VALID",
                ))
            session.add(HypothesisStep(
                id=uuid4(), incident_id=own.id, step_no=1,
                model_id="deterministic-baseline-v1", prompt_version="none",
                evidence_ids=[
                    str(own_evidence_id), str(foreign_evidence_id),
                    str(uuid4()), "not-a-uuid",
                ],
                cause_scores={}, probe_id="simulator.health",
                decision="ABSTAINED", token_reserved=0, token_used=0,
            ))
    async with sessions_fixture() as session:
        page = await read_lineage_page(
            session, "alpha", own.id, "hypothesis",
        )
        assert len(page["nodes"]) == 1
        assert page["links_truncated"] is False
        assert any(
            edge["relation"] == "CITES"
            and edge["to"] == f"evidence:{own_evidence_id}"
            for edge in page["edges"]
        )
        assert not any(
            edge["to"] == f"evidence:{foreign_evidence_id}"
            for edge in page["edges"]
        )
        assert len(page["missing_refs"]) == 3
        assert str(foreign_evidence_id) not in str(page["missing_refs"])
        summary = await build_lineage(session, "alpha", own.id)
        assert str(foreign_evidence_id) not in str(summary)
        assert "not-a-uuid" not in str(summary)
        assert summary["projection"] == "PARTIAL_LINKS"

    async with sessions_fixture() as session:
        async with session.begin():
            step = await session.scalar(
                select(HypothesisStep).where(HypothesisStep.incident_id == own.id)
            )
            step.evidence_ids = [str(own_evidence_id)] * 205
    async with sessions_fixture() as session:
        capped = await read_lineage_page(session, "alpha", own.id, "hypothesis")
        assert capped["links_truncated"] is True
        assert len(capped["edges"]) <= 201
        summary = await build_lineage(session, "alpha", own.id)
        assert summary["links_truncated"] is True
        assert summary["projection"] == "PARTIAL_LINKS"
        assert len([edge for edge in summary["edges"] if edge["relation"] == "CITES"]) <= 200



@pytest.mark.asyncio
async def test_redteam_lineage_page_api_rejects_cross_tenant_and_unbounded_queries(
    sessions_fixture,
):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "paged-api", IncidentCreate(target_id="checkout"),
        )

    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="paged-lineage-test-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            url = f"/v1/incidents/{incident.id}/lineage/records"
            own = {"X-Tenant-Id": "t", "X-OpsBlaze-Token": "paged-lineage-test-token"}
            result = await client.get(url, params={"kind": "evidence", "limit": 10},
                                      headers=own)
            assert result.status_code == 200
            assert result.json()["nodes"] == []
            assert result.json()["next_cursor"] is None
            wrong = {**own, "X-Tenant-Id": "other"}
            assert (await client.get(url, params={"kind": "evidence"},
                                     headers=wrong)).status_code == 404
            assert (await client.get(url, params={"kind": "effect", "limit": 51},
                                     headers=own)).status_code == 422
            assert (await client.get(url, params={"kind": "unbounded-internal"},
                                     headers=own)).status_code == 422
            assert (await client.get(url, params={
                "kind": "evidence", "cursor": "not-a-uuid",
            }, headers=own)).status_code == 422
    finally:
        app.dependency_overrides.clear()

@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["private-content", {"secret": "private-content"}, 42])
async def test_redteam_malformed_summary_reference_list_is_redacted(sessions_fixture, malformed):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "bad-ref-list", IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(HypothesisStep(
                id=uuid4(), incident_id=incident.id, step_no=1,
                model_id="deterministic-baseline-v1", prompt_version="none",
                evidence_ids=malformed, cause_scores={}, probe_id="simulator.health",
                decision="ABSTAINED", token_reserved=0, token_used=0,
            ))
    async with sessions_fixture() as session:
        summary = await build_lineage(session, "t", incident.id)
        page = await read_lineage_page(session, "t", incident.id, "hypothesis")
        for graph in (summary, page):
            assert "private-content" not in str(graph)
            assert any(r["missing_target"] == "invalid-evidence-list" for r in graph["missing_refs"])
        assert summary["projection"] == "PARTIAL_LINKS"


@pytest.mark.asyncio
async def test_redteam_all_nine_record_kinds_membership_parity_detail_and_diagnostics(
    sessions_fixture,
):
    """P3-06, P3-16, P3-17, P3-19: full 9-kind membership parity, scoped detail & claim distinction."""
    from datetime import datetime, timedelta, timezone
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.lineage import read_lineage_record_detail
    from backend.lineage_projection import rebuild_evidence_page
    from backend.models import (
        ApprovalGrant, EffectReceipt, Evidence, ExperienceCapsule,
        FailureCase, FailureCluster, IncidentPlan, OperationalLesson,
        OutcomeRevision,
    )
    from backend.settings import Settings, get_settings

    now = datetime.now(timezone.utc).replace(microsecond=0)
    async with sessions_fixture() as session:
        own = await admit_incident(session, "alpha", "parity-own", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        foreign = await admit_incident(session, "beta", "parity-foreign", IncidentCreate(target_id="secret-svc"))

    ev_id, hyp_id, plan_id, grant_id = uuid4(), uuid4(), uuid4(), uuid4()
    eff_id, out_id, cap_id, lesson_id, cluster_id, fail_id = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
    )
    foreign_ev_id = uuid4()
    digest = "a" * 64

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(Evidence(
                id=ev_id, tenant_id="alpha", incident_id=own.id, target_id="checkout",
                source="simulator.health", source_version="simulator-v1", target_version=1,
                observed_at=now, sha256="b" * 64, redacted_excerpt='{"healthy":false}',
                classification="INTERNAL", validity="VALID",
            ))
            session.add(Evidence(
                id=foreign_ev_id, tenant_id="beta", incident_id=foreign.id, target_id="secret-svc",
                source="simulator.health", source_version="simulator-v1", target_version=1,
                observed_at=now, sha256="c" * 64, redacted_excerpt='{"secret":"nope"}',
                classification="INTERNAL", validity="VALID",
            ))
            session.add(HypothesisStep(
                id=hyp_id, incident_id=own.id, step_no=1,
                model_id="deterministic-baseline-v1", prompt_version="v1",
                evidence_ids=[str(ev_id)], cause_scores={"simulated_target_unhealthy": 90},
                probe_id="simulator.health", decision="PROPOSED",
                token_reserved=0, token_used=0,
            ))
            session.add(IncidentPlan(
                id=plan_id, tenant_id="alpha", incident_id=own.id, target_id="checkout",
                action_id="SIMULATOR_MARK_HEALTHY", plan_digest=digest, expected_version=1,
                evidence_ids=[str(ev_id)], status="APPROVED", version=2,
                decided_at=now, decided_by="DEV_REVIEWER", review_epoch=1,
            ))
            session.add(ApprovalGrant(
                id=grant_id, tenant_id="alpha", plan_id=plan_id, plan_digest=digest,
                reviewer_id="DEV_REVIEWER", scope="SIMULATOR_MARK_HEALTHY",
                decision="APPROVE", state="CONSUMED", reason="verified synthetic plan",
                version=2, expires_at=now + timedelta(minutes=5),
            ))
            session.add(EffectReceipt(
                id=eff_id, tenant_id="alpha", incident_id=own.id, plan_digest=digest,
                target_id="checkout", adapter_id="simulator", idempotency_key=str(eff_id),
                fence=1, expected_version=1, state="UNKNOWN", attempt_count=1,
            ))
            session.add(OutcomeRevision(
                id=out_id, tenant_id="alpha", incident_id=own.id, effect_id=eff_id,
                revision=1, label="DISPUTED", witness_source="REVIEWER_DISPUTE",
                observed_version=1, reason="independent check disagreed", reviewer_id="rev-1",
            ))
            session.add(ExperienceCapsule(
                id=cap_id, tenant_id="alpha", incident_id=own.id, outcome_revision_id=out_id,
                consent_epoch=1, tenant_consent_epoch=1, target_digest="d" * 64,
                evidence_ids=[str(ev_id)], status="ACTIVE", expires_at=now + timedelta(days=7),
            ))
            session.add(OperationalLesson(
                id=lesson_id, tenant_id="alpha", source_capsule_id=cap_id,
                source_outcome_id=out_id, target_digest="d" * 64, rule_id="rule-1",
                summary_redacted="synthetic check", consent_epoch=1, status="DRAFT",
                version=1, valid_from=now, valid_until=now + timedelta(days=7),
            ))
            session.add(FailureCluster(
                id=cluster_id, tenant_id="alpha", signature="sig-1", version=1, case_count=1,
            ))
            session.add(FailureCase(
                id=fail_id, tenant_id="alpha", incident_id=own.id, outcome_revision_id=out_id,
                cluster_id=cluster_id, origin_code="EXECUTION_FAILURE", first_bad_step=1,
                classification_version=1, review_status="OPEN",
            ))

    # Rebuild projected causal edges for all projected source kinds.
    async with sessions_fixture() as session:
        async with session.begin():
            for kind in ("hypothesis", "plan", "approval", "effect", "outcome", "experience", "lesson", "failure"):
                await rebuild_evidence_page(session, "alpha", own.id, kind)

    expected_ids = {
        "evidence": ev_id, "hypothesis": hyp_id, "plan": plan_id,
        "approval": grant_id, "effect": eff_id, "outcome": out_id,
        "experience": cap_id, "lesson": lesson_id, "failure": fail_id,
    }
    async with sessions_fixture() as session:
        summary = await build_lineage(session, "alpha", own.id)
        assert summary["projection"] == "COMPLETE"
        assert summary["evidence_projection"] == "MATCHED"
        assert summary["diagnostics"]["missing_edge_count"] == 0
        # Incident + outcome are trace-eligible; neither has a TraceRef in this fixture.
        assert summary["diagnostics"]["context_loss_count"] == 2
        assert summary["diagnostics"]["projection_lag_seconds"] == 0

        summary_membership = {
            (e["from"], e["to"]) for e in summary["edges"] if e["relation"] == "BELONGS_TO"
        }
        for kind, rec_id in expected_ids.items():
            assert (f"{kind}:{rec_id}", f"incident:{own.id}") in summary_membership
            page = await read_lineage_page(session, "alpha", own.id, kind)
            assert [n["id"] for n in page["nodes"]] == [f"{kind}:{rec_id}"]
            assert any(
                e == {"from": f"{kind}:{rec_id}", "to": f"incident:{own.id}", "relation": "BELONGS_TO"}
                for e in page["edges"]
            ), kind
            assert page["evidence_projection"] in {"MATCHED", "NOT_APPLICABLE"}

            detail = await read_lineage_record_detail(session, "alpha", own.id, kind, rec_id)
            assert detail is not None
            assert detail["id"] == f"{kind}:{rec_id}"
            assert "secret-svc" not in str(detail)
            # Foreign tenant cannot read own record; own tenant cannot read foreign record.
            assert await read_lineage_record_detail(session, "beta", own.id, kind, rec_id) is None
        assert await read_lineage_record_detail(session, "alpha", own.id, "evidence", foreign_ev_id) is None

        hyp_detail = await read_lineage_record_detail(session, "alpha", own.id, "hypothesis", hyp_id)
        assert hyp_detail["authority_kind"] == "MODEL_OR_INVESTIGATION_CLAIM"
        assert hyp_detail["verified_outcome"] is False
        assert hyp_detail["details"]["claim_only"] is True
        assert hyp_detail["details"]["verified_recovery"] is False

        eff_detail = await read_lineage_record_detail(session, "alpha", own.id, "effect", eff_id)
        assert eff_detail["authority_kind"] == "EFFECT_LEDGER"
        assert eff_detail["unresolved_or_failed"] is True

        out_detail = await read_lineage_record_detail(session, "alpha", own.id, "outcome", out_id)
        assert out_detail["authority_kind"] == "INDEPENDENT_OUTCOME"
        assert out_detail["verified_outcome"] is False
        assert out_detail["unresolved_or_failed"] is True

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="detail-token")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            own_headers = {"X-Tenant-Id": "alpha", "X-OpsBlaze-Token": "detail-token"}
            res = await client.get(
                f"/v1/incidents/{own.id}/lineage/record/hypothesis/{hyp_id}",
                headers=own_headers,
            )
            assert res.status_code == 200
            assert res.json()["authority_kind"] == "MODEL_OR_INVESTIGATION_CLAIM"
            # Foreign tenant blocked.
            assert (await client.get(
                f"/v1/incidents/{own.id}/lineage/record/hypothesis/{hyp_id}",
                headers={**own_headers, "X-Tenant-Id": "beta"},
            )).status_code == 404
            # Invalid kind rejected.
            assert (await client.get(
                f"/v1/incidents/{own.id}/lineage/record/unknown/{hyp_id}",
                headers=own_headers,
            )).status_code == 422
    finally:
        app.dependency_overrides.clear()
