"""Security-admin revocations that fence new reviewer grants and effects."""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    ApprovalGrant, ApprovalRevocation, IdentityProviderEvent, IncidentPlan,
    RevokedIdentity,
)


async def lock_reviewer_subject(
    session: AsyncSession, tenant_id: str, actor_id: str,
) -> None:
    """Serialize grant creation against tenant-scoped security revocation."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(902103, hashtext(:actor))"),
            {"actor": f"{tenant_id}:{actor_id}"},
        )


async def revoke_identity(
    session: AsyncSession,
    tenant_id: str,
    actor_id: str,
    security_admin: str,
    reason: str,
    *, event_digest: str | None = None,
) -> tuple[RevokedIdentity, int]:
    """Permanent deny-list record and all still-pending grants in one commit."""
    if actor_id == security_admin:
        raise ValueError("security administrator cannot self-revoke")
    if not 8 <= len(reason.strip()) <= 256:
        raise ValueError("revocation reason must contain 8–256 characters")
    when = datetime.now(timezone.utc)
    async with session.begin():
        await lock_reviewer_subject(session, tenant_id, actor_id)
        if event_digest is not None:
            if (
                len(event_digest) != 64
                or any(ch not in "0123456789abcdef" for ch in event_digest)
            ):
                raise ValueError("invalid signed event digest")
            previous = await session.get(
                IdentityProviderEvent, event_digest, with_for_update=True
            )
            if previous is not None:
                if previous.tenant_id != tenant_id or previous.actor_id != actor_id:
                    raise ValueError("security event identity mismatch")
                revoked = await session.get(RevokedIdentity, (tenant_id, actor_id))
                if revoked is None:
                    raise ValueError("security event ledger has no revocation")
                return revoked, 0

        revoked = await session.get(RevokedIdentity, (tenant_id, actor_id))
        if revoked is not None:
            if event_digest is not None:
                session.add(IdentityProviderEvent(
                    event_digest=event_digest,
                    tenant_id=tenant_id, actor_id=actor_id,
                ))
            return revoked, 0

        grants = (await session.scalars(
            select(ApprovalGrant)
            .where(
                ApprovalGrant.tenant_id == tenant_id,
                ApprovalGrant.reviewer_id == actor_id,
                ApprovalGrant.state == "APPROVED",
            )
            .with_for_update()
        )).all()
        for grant in grants:
            grant.state = "REVOKED"
            grant.revoked_at = when
            grant.version += 1
            plan = await session.scalar(
                select(IncidentPlan)
                .where(
                    IncidentPlan.id == grant.plan_id,
                    IncidentPlan.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if plan is not None and plan.status == "APPROVED":
                plan.status = "REVOKED"
                plan.version += 1
            session.add(ApprovalRevocation(
                id=uuid5(NAMESPACE_URL, f"opsblaze/security-revoke/{grant.id}"),
                tenant_id=tenant_id, approval_id=grant.id,
                revoked_by=security_admin, reason=reason.strip(),
            ))

        revoked = RevokedIdentity(
            tenant_id=tenant_id, actor_id=actor_id,
            revoked_at=when, revoked_by=security_admin, reason=reason.strip(),
        )
        session.add(revoked)
        if event_digest is not None:
            session.add(IdentityProviderEvent(
                event_digest=event_digest, tenant_id=tenant_id,
                actor_id=actor_id,
            ))
    return revoked, len(grants)
