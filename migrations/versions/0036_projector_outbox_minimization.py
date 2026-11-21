"""Hide Temporal outbox rows from the restricted E1 projector.

The only cross-domain read is a boolean ordering check, executed as the
migration/table owner. It returns no signal contents or identifiers. The
operator must reapply scripts/projector_grants.sql to activate the scoped
read policy for the separate PostgreSQL projector role.
"""

from alembic import op

revision = "0036_projector_outbox_rls"
down_revision = "0035_projector_lock_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.projector_intent_ready(
            p_incident uuid, p_intent bigint
        )
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$
            SELECT
                EXISTS (
                    SELECT 1 FROM public.outbox current_intent
                    WHERE current_intent.id = p_intent
                      AND current_intent.incident_id = p_incident
                      AND current_intent.kind = 'PROJECT_EXPERIENCE'
                )
                AND EXISTS (
                    SELECT 1 FROM public.outbox start_intent
                    WHERE start_intent.incident_id = p_incident
                      AND start_intent.kind = 'START_WORKFLOW'
                      AND start_intent.delivered_at IS NOT NULL
                )
                AND NOT EXISTS (
                    SELECT 1 FROM public.outbox older_intent
                    WHERE older_intent.incident_id = p_incident
                      AND older_intent.id < p_intent
                      AND older_intent.delivered_at IS NULL
                      AND older_intent.dead_lettered_at IS NULL
                )
        $function$;
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.projector_intent_ready(uuid, bigint)"
        " FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION public.projector_intent_ready(uuid, bigint)"
    )
