-- E1 PostgreSQL role scope. Run *after* alembic upgrade head.
-- DBA creates LOGIN opsblaze_projector with a separately stored secret first.
-- Do not use the owning/migration/API role in the projector worker.
-- Grants only source reads, local capsule inserts and bounded outbox ACKs.
-- For SELECT FOR UPDATE, the inert projector_lock_marker is the only
-- writable field on incident and consent records.
--
-- RLS hides Temporal outbox rows (including their payloads). A migration-owned
-- boolean probe checks earlier signal completion without exposing those rows.
-- This protects Temporal payloads, NOT all incident/evidence source columns.
-- The role must be non-superuser, non-owner and must not have BYPASSRLS.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'opsblaze_projector'
    ) THEN
        RAISE EXCEPTION 'Create a separate opsblaze_projector LOGIN role first';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'opsblaze_projector'
        AND (rolsuper OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Projector role must not bypass row security';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO opsblaze_projector;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM opsblaze_projector;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM opsblaze_projector;

GRANT SELECT ON TABLE
    public.incident,
    public.outbox,
    public.outcome_revision,
    public.evidence,
    public.experience_consent,
    public.tenant_experience_consent,
    public.experience_capsule
TO opsblaze_projector;

GRANT UPDATE (projector_lock_marker) ON TABLE
    public.incident,
    public.experience_consent,
    public.tenant_experience_consent
TO opsblaze_projector;

GRANT INSERT ON TABLE public.experience_capsule TO opsblaze_projector;

GRANT UPDATE (
    attempt, available_at, delivered_at, dead_lettered_at, dead_letter_code
) ON TABLE public.outbox TO opsblaze_projector;

GRANT SELECT, INSERT ON TABLE public.worker_heartbeat TO opsblaze_projector;
GRANT UPDATE (last_seen) ON TABLE public.worker_heartbeat TO opsblaze_projector;

-- A pending local projector may not turn itself into a Temporal signal.
-- The owning application/migration role bypasses RLS as table owner.
ALTER TABLE public.outbox ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS projector_outbox_read ON public.outbox;
CREATE POLICY projector_outbox_read ON public.outbox
    FOR SELECT TO opsblaze_projector
    USING (kind = 'PROJECT_EXPERIENCE');

-- Function owner sees the complete ordering ledger. The projector gets only
-- a boolean answer, not earlier Temporal rows or signal payloads.
GRANT EXECUTE ON FUNCTION public.projector_intent_ready(uuid, bigint)
    TO opsblaze_projector;

DROP POLICY IF EXISTS projector_outbox_update ON public.outbox;
CREATE POLICY projector_outbox_update ON public.outbox
    FOR UPDATE TO opsblaze_projector
    USING (kind = 'PROJECT_EXPERIENCE')
    WITH CHECK (kind = 'PROJECT_EXPERIENCE');
