"""Project experience provenance in the capsule's source transaction."""

from alembic import op

revision = "0046_experience_edge"
down_revision = "0045_learning_lineage"
branch_labels = None
depends_on = None


def _insert_edge(prefix: str) -> str:
    return f"""
        INSERT INTO {prefix}lineage_edge
            (tenant_id, incident_id, from_type, from_id, to_type, to_id, relation)
        SELECT NEW.tenant_id, NEW.incident_id, 'experience', NEW.id,
               'outcome', outcome.id, 'DERIVED_FROM'
        FROM {prefix}incident AS incident
        JOIN {prefix}outcome_revision AS outcome
          ON outcome.incident_id = incident.id AND outcome.tenant_id = incident.tenant_id
        WHERE incident.id = NEW.incident_id AND incident.tenant_id = NEW.tenant_id
          AND outcome.id = NEW.outcome_revision_id
        ON CONFLICT (tenant_id, incident_id, from_type, from_id, to_type, to_id, relation)
        DO NOTHING;
    """


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(f"""
            CREATE FUNCTION public.project_experience_lineage() RETURNS trigger
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $function$
            BEGIN
                IF TG_RELID <> 'public.experience_capsule'::regclass OR TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'experience lineage requires a capsule insert';
                END IF;
                {_insert_edge("public.")}
                RETURN NULL;
            END;
            $function$;
        """)
        op.execute("REVOKE ALL ON FUNCTION public.project_experience_lineage() FROM PUBLIC")
        # Custom default privileges can also grant EXECUTE to named roles.
        op.execute("""
            DO $acl$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_catalog.pg_proc AS f
                    CROSS JOIN LATERAL pg_catalog.aclexplode(f.proacl) AS acl
                    WHERE f.oid = 'public.project_experience_lineage()'::regprocedure
                      AND acl.grantee <> f.proowner
                ) THEN
                    RAISE EXCEPTION 'experience lineage function must have owner-only access';
                END IF;
            END;
            $acl$;
        """)
        op.execute("""
            CREATE TRIGGER project_experience_lineage
            AFTER INSERT ON public.experience_capsule FOR EACH ROW
            EXECUTE FUNCTION public.project_experience_lineage();
        """)
    elif dialect == "sqlite":
        op.execute(f"""
            CREATE TRIGGER project_experience_lineage
            AFTER INSERT ON experience_capsule FOR EACH ROW
            BEGIN
                {_insert_edge("")}
            END;
        """)
    else:
        raise NotImplementedError(f"experience lineage migration does not support {dialect}")


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER project_experience_lineage ON public.experience_capsule")
        op.execute("DROP FUNCTION public.project_experience_lineage()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER project_experience_lineage")
    else:
        raise NotImplementedError(f"experience lineage migration does not support {dialect}")
