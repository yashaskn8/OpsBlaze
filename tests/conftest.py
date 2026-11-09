"""Shared pytest fixtures for OpsBlaze B1 test suite.

Non-destructive PostgreSQL test isolation:
- Uses `opsblaze_test` database without dropping or truncating existing foundation rows.
- Creates a unique test tenant per test or test module and cleans up only the rows created
  under that test tenant if needed, or isolates by unique UUIDs so existing 22 rows remain intact.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings, get_settings
from backend.domain.incident import DegradedRecoveryGate
from backend.observability.metrics import metrics
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import (
    EffectReceipt,
    Incident,
    Outbox,
    ResourceEpoch,
    ResourceReservation,
    Tenant,
)
from backend.persistence.repositories import IncidentRepository


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics.reset_for_tests()
    DegradedRecoveryGate.exit_degraded_mode()


@pytest.fixture
def test_settings() -> Settings:
    base = get_settings()
    return base.model_copy(update={"postgres_db": "opsblaze_test"})


@pytest.fixture
async def db_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(test_settings, pool_size=10, max_overflow=10, pool_timeout=5.0)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(db_engine)


@pytest.fixture
async def isolated_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[uuid.UUID]:
    """Create a fresh tenant in opsblaze_test and clean up ONLY its rows on teardown,
    leaving all pre-existing rows in opsblaze_test 100% untouched.
    """
    tenant_id = uuid.uuid4()
    repo = IncidentRepository(session_factory)
    await repo.ensure_tenant(tenant_id, f"b1-test-tenant-{tenant_id}")
    try:
        yield tenant_id
    finally:
        async with session_factory() as session:
            # Clean up only rows belonging to this isolated test tenant
            inc_ids_subq = select_incident_ids_for_tenant(tenant_id)
            from backend.persistence.models import (
                ApprovalAudit,
                ApprovalRequest,
                BudgetLedger,
                CandidateEvaluationManifest,
                CandidateEvaluationReport,
                CandidateEvaluationTrial,
                CapabilityGrant,
                ConsentEpoch,
                EvidenceLedger,
                ExperienceAudit,
                ExperienceCapsule,
                FailureCase,
                FailureCluster,
                IdentityMembership,
                ImprovementProposal,
                InvestigationStep,
                LessonUsage,
                OperationalLesson,
                OutcomeObservation,
                OutcomeRevision,
                PolicyBundle,
                PolicyRelease,
                ReleaseEventAudit,
                ToolCall,
            )

            await session.execute(delete(IdentityMembership).where(IdentityMembership.tenant_id == tenant_id))
            await session.execute(delete(ReleaseEventAudit).where(ReleaseEventAudit.tenant_id == tenant_id))
            await session.execute(delete(CandidateEvaluationReport).where(CandidateEvaluationReport.tenant_id == tenant_id))
            await session.execute(delete(CandidateEvaluationTrial).where(CandidateEvaluationTrial.tenant_id == tenant_id))
            await session.execute(delete(CandidateEvaluationManifest).where(CandidateEvaluationManifest.tenant_id == tenant_id))
            await session.execute(delete(LessonUsage).where(LessonUsage.tenant_id == tenant_id))
            await session.execute(delete(OperationalLesson).where(OperationalLesson.tenant_id == tenant_id))
            await session.execute(delete(ImprovementProposal).where(ImprovementProposal.tenant_id == tenant_id))
            await session.execute(delete(FailureCase).where(FailureCase.tenant_id == tenant_id))
            await session.execute(delete(FailureCluster).where(FailureCluster.tenant_id == tenant_id))
            await session.execute(delete(ExperienceAudit).where(ExperienceAudit.tenant_id == tenant_id))
            await session.execute(delete(ExperienceCapsule).where(ExperienceCapsule.tenant_id == tenant_id))
            await session.execute(delete(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id))
            await session.execute(delete(OutcomeRevision).where(OutcomeRevision.tenant_id == tenant_id))
            await session.execute(delete(OutcomeObservation).where(OutcomeObservation.tenant_id == tenant_id))
            await session.execute(delete(ApprovalAudit).where(ApprovalAudit.tenant_id == tenant_id))
            await session.execute(delete(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id))
            await session.execute(delete(ToolCall).where(ToolCall.tenant_id == tenant_id))
            await session.execute(delete(CapabilityGrant).where(CapabilityGrant.tenant_id == tenant_id))
            await session.execute(delete(EvidenceLedger).where(EvidenceLedger.tenant_id == tenant_id))
            await session.execute(delete(InvestigationStep).where(InvestigationStep.tenant_id == tenant_id))
            await session.execute(delete(BudgetLedger).where(BudgetLedger.tenant_id == tenant_id))
            await session.execute(delete(EffectReceipt).where(EffectReceipt.tenant_id == tenant_id))
            await session.execute(
                delete(ResourceReservation).where(ResourceReservation.tenant_id == tenant_id)
            )
            await session.execute(delete(ResourceEpoch).where(ResourceEpoch.tenant_id == tenant_id))
            await session.execute(
                delete(Outbox).where(
                    (Outbox.incident_id.in_(inc_ids_subq))
                    | (Outbox.payload["tenant_id"].as_string() == str(tenant_id))
                )
            )
            await session.execute(delete(Incident).where(Incident.tenant_id == tenant_id))
            await session.execute(delete(PolicyRelease).where(PolicyRelease.tenant_id == tenant_id))
            await session.execute(delete(PolicyBundle).where(PolicyBundle.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()


def select_incident_ids_for_tenant(tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    return select(Incident.id).where(Incident.tenant_id == tenant_id)


@pytest.fixture
async def second_isolated_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[uuid.UUID]:
    tenant_id = uuid.uuid4()
    repo = IncidentRepository(session_factory)
    await repo.ensure_tenant(tenant_id, f"b1-test-tenant-2-{tenant_id}")
    try:
        yield tenant_id
    finally:
        async with session_factory() as session:
            inc_ids_subq = select_incident_ids_for_tenant(tenant_id)
            from backend.persistence.models import (
                ApprovalAudit,
                ApprovalRequest,
                CandidateEvaluationManifest,
                CandidateEvaluationReport,
                CandidateEvaluationTrial,
                ConsentEpoch,
                ExperienceAudit,
                ExperienceCapsule,
                FailureCase,
                FailureCluster,
                ImprovementProposal,
                LessonUsage,
                OperationalLesson,
                OutcomeObservation,
                OutcomeRevision,
            )

            await session.execute(delete(CandidateEvaluationReport).where(CandidateEvaluationReport.tenant_id == tenant_id))
            await session.execute(delete(CandidateEvaluationTrial).where(CandidateEvaluationTrial.tenant_id == tenant_id))
            await session.execute(delete(CandidateEvaluationManifest).where(CandidateEvaluationManifest.tenant_id == tenant_id))
            await session.execute(delete(LessonUsage).where(LessonUsage.tenant_id == tenant_id))
            await session.execute(delete(OperationalLesson).where(OperationalLesson.tenant_id == tenant_id))
            await session.execute(delete(ImprovementProposal).where(ImprovementProposal.tenant_id == tenant_id))
            await session.execute(delete(FailureCase).where(FailureCase.tenant_id == tenant_id))
            await session.execute(delete(FailureCluster).where(FailureCluster.tenant_id == tenant_id))
            await session.execute(delete(ExperienceAudit).where(ExperienceAudit.tenant_id == tenant_id))
            await session.execute(delete(ExperienceCapsule).where(ExperienceCapsule.tenant_id == tenant_id))
            await session.execute(delete(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id))
            await session.execute(delete(OutcomeRevision).where(OutcomeRevision.tenant_id == tenant_id))
            await session.execute(delete(OutcomeObservation).where(OutcomeObservation.tenant_id == tenant_id))
            await session.execute(delete(ApprovalAudit).where(ApprovalAudit.tenant_id == tenant_id))
            await session.execute(delete(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id))
            await session.execute(delete(EffectReceipt).where(EffectReceipt.tenant_id == tenant_id))
            await session.execute(
                delete(ResourceReservation).where(ResourceReservation.tenant_id == tenant_id)
            )
            await session.execute(delete(ResourceEpoch).where(ResourceEpoch.tenant_id == tenant_id))
            await session.execute(
                delete(Outbox).where(
                    (Outbox.incident_id.in_(inc_ids_subq))
                    | (Outbox.payload["tenant_id"].as_string() == str(tenant_id))
                )
            )
            await session.execute(delete(Incident).where(Incident.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()


@pytest.fixture
async def api_client(test_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(test_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        try:
            yield client
        finally:
            if getattr(app.state, "engine", None) is not None:
                await app.state.engine.dispose()
