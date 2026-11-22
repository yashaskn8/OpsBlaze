"""Intent-scoped source visibility for the restricted E1 projector.

The migration owner is the incident/outbox owner; these probes reveal only a
boolean that a local projection remains pending. The worker cannot use either
to read source rows for incidents without an active projection intent.
"""

from alembic import op

revision = "0037_projector_source_rls"
down_revision = "0036_projector_outbox_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.projector_source_visible(
            p_incident uuid, p_tenant varchar
        )
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.incident i
                JOIN public.outbox o ON o.incident_id = i.id
                WHERE i.id = p_incident
                  AND i.tenant_id = p_tenant
                  AND o.kind = 'PROJECT_EXPERIENCE'
                  AND o.delivered_at IS NULL
                  AND o.dead_lettered_at IS NULL
            )
        $function$;
    """)
    op.execute("""
        CREATE FUNCTION public.projector_tenant_visible(
            p_tenant varchar
        )
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT EXISTS (
                SELECT 1
                FROM public.incident i
                JOIN public.outbox o ON o.incident_id = i.id
                WHERE i.tenant_id = p_tenant
                  AND o.kind = 'PROJECT_EXPERIENCE'
                  AND o.delivered_at IS NULL
                  AND o.dead_lettered_at IS NULL
            )
        $function$;
    """)
    for signature in (
        "public.projector_source_visible(uuid, varchar)",
        "public.projector_tenant_visible(varchar)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.projector_tenant_visible(varchar)")
    op.execute("DROP FUNCTION public.projector_source_visible(uuid, varchar)")
