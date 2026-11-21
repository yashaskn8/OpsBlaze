-- E1 PostgreSQL role scope. Run *after* alembic upgrade head.
-- DBA creates LOGIN opsblaze_projector with a separately stored secret first.
-- Do not use the owning/migration/API role in the projector worker.
-- Grants only source reads, local capsule inserts and bounded outbox ACKs.
-- For SELECT FOR UPDATE, the inert projector_lock_marker is the only
-- writable field on incident and consent records.
--
-- SECURITY LIMIT: this role can still READ all outbox rows to preserve
-- per-incident ordering. Full row-level secrecy requires a separate queue.
-- It can UPDATE only PROJECT_EXPERIENCE rows due to RLS below.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'opsblaze_projector'
    ) THEN
        RAISE EXCEPTION 'Create a separate opsblaze_projector LOGIN role first';
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
    FOR SELECT TO opsblaze_projector USING (true);

DROP POLICY IF EXISTS projector_outbox_update ON public.outbox;
CREATE POLICY projector_outbox_update ON public.outbox
    FOR UPDATE TO opsblaze_projector
    USING (kind = 'PROJECT_EXPERIENCE')
    WITH CHECK (kind = 'PROJECT_EXPERIENCE');
