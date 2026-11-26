-- E1 PostgreSQL role scope. Run *after* alembic upgrade head.
-- DBA creates LOGIN opsblaze_projector with a separately stored secret first.
-- Do not use the owning/migration/API role in the projector worker.
-- Grants only source reads, local capsule inserts and bounded outbox ACKs.
-- For SELECT FOR UPDATE, the inert projector_lock_marker is the only
-- writable field on incident and consent records.
--
-- RLS hides Temporal outbox rows (including their payloads). A migration-owned
-- boolean probe checks earlier signal completion without exposing those rows.
-- Only required source columns are readable; source excerpts and outcome
-- reasons remain inaccessible even if the projector process is compromised.
-- Source-row policies expose metadata only while a local projection intent
-- is pending. A compromised role can still see other tenants with pending
-- projection jobs; per-tenant credentials are a separate production gate.
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

-- A --no-acl restore reinstates PostgreSQL's default PUBLIC EXECUTE.
-- Reapply the trigger function restriction before starting the projector.
REVOKE ALL ON FUNCTION public.project_experience_lineage() FROM PUBLIC, opsblaze_projector;

-- Source reads are column-limited. Adding a source field must require a
-- deliberate grant and an updated PostgreSQL negative regression test.
GRANT SELECT (id, tenant_id, target_id)
    ON TABLE public.incident TO opsblaze_projector;
GRANT SELECT (id, tenant_id, incident_id, revision, label)
    ON TABLE public.outcome_revision TO opsblaze_projector;
GRANT SELECT (id, tenant_id, incident_id, validity, captured_at)
    ON TABLE public.evidence TO opsblaze_projector;

-- Local-only projection bookkeeping still requires the full projection row
-- and consent/capsule metadata. Temporal outbox rows stay hidden by RLS.
GRANT SELECT ON TABLE
    public.outbox,
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

-- Each source row must belong to an incident with an outstanding local
-- projection intent. Delivered and terminal intents confer no read rights.
-- The SECURITY DEFINER predicates are migration-owned and bypass only source
-- policy evaluation; they return no incident data or Temporal payload.
GRANT EXECUTE ON FUNCTION public.projector_source_visible(uuid, varchar)
    TO opsblaze_projector;
GRANT EXECUTE ON FUNCTION public.projector_tenant_visible(varchar)
    TO opsblaze_projector;

ALTER TABLE public.incident ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outcome_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.experience_consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_experience_consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.experience_capsule ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS projector_incident_read ON public.incident;
CREATE POLICY projector_incident_read ON public.incident
    FOR SELECT TO opsblaze_projector
    USING (public.projector_source_visible(id, tenant_id));
DROP POLICY IF EXISTS projector_incident_lock ON public.incident;
CREATE POLICY projector_incident_lock ON public.incident
    FOR UPDATE TO opsblaze_projector
    USING (public.projector_source_visible(id, tenant_id))
    WITH CHECK (public.projector_source_visible(id, tenant_id));

DROP POLICY IF EXISTS projector_outcome_read ON public.outcome_revision;
CREATE POLICY projector_outcome_read ON public.outcome_revision
    FOR SELECT TO opsblaze_projector
    USING (public.projector_source_visible(incident_id, tenant_id));

DROP POLICY IF EXISTS projector_evidence_read ON public.evidence;
CREATE POLICY projector_evidence_read ON public.evidence
    FOR SELECT TO opsblaze_projector
    USING (public.projector_source_visible(incident_id, tenant_id));

DROP POLICY IF EXISTS projector_incident_consent_read ON public.experience_consent;
CREATE POLICY projector_incident_consent_read ON public.experience_consent
    FOR SELECT TO opsblaze_projector
    USING (public.projector_source_visible(incident_id, tenant_id));
DROP POLICY IF EXISTS projector_incident_consent_lock ON public.experience_consent;
CREATE POLICY projector_incident_consent_lock ON public.experience_consent
    FOR UPDATE TO opsblaze_projector
    USING (public.projector_source_visible(incident_id, tenant_id))
    WITH CHECK (public.projector_source_visible(incident_id, tenant_id));

DROP POLICY IF EXISTS projector_tenant_consent_read ON public.tenant_experience_consent;
CREATE POLICY projector_tenant_consent_read ON public.tenant_experience_consent
    FOR SELECT TO opsblaze_projector
    USING (public.projector_tenant_visible(tenant_id));
DROP POLICY IF EXISTS projector_tenant_consent_lock ON public.tenant_experience_consent;
CREATE POLICY projector_tenant_consent_lock ON public.tenant_experience_consent
    FOR UPDATE TO opsblaze_projector
    USING (public.projector_tenant_visible(tenant_id))
    WITH CHECK (public.projector_tenant_visible(tenant_id));

DROP POLICY IF EXISTS projector_capsule_read ON public.experience_capsule;
CREATE POLICY projector_capsule_read ON public.experience_capsule
    FOR SELECT TO opsblaze_projector
    USING (public.projector_source_visible(incident_id, tenant_id));
DROP POLICY IF EXISTS projector_capsule_insert ON public.experience_capsule;
CREATE POLICY projector_capsule_insert ON public.experience_capsule
    FOR INSERT TO opsblaze_projector
    WITH CHECK (
        status = 'VALID'
        AND public.projector_source_visible(incident_id, tenant_id)
    );
