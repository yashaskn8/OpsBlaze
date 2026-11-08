"""Apply and verify Stage 11 migration on both databases without data loss."""

import os
import sys

sys.path.insert(0, ".")
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from backend.config import get_settings


def migrate_database(db_name: str) -> None:
    print("\n==================================================")
    print(f"Migrating database: {db_name}")
    print("==================================================")

    os.environ["POSTGRES_DB"] = db_name
    settings = get_settings()
    sync_url = settings.database_url.set(drivername="postgresql+psycopg2", database=db_name).render_as_string(hide_password=False)

    engine = create_engine(sync_url)
    with engine.connect() as conn:
        current_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        print(f"Current revision before upgrade: {current_rev}")

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        new_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        print(f"New revision after upgrade: {new_rev}")

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        stage11_tables = [
            "policy_bundle",
            "policy_release",
            "release_event_audit",
        ]
        for t in stage11_tables:
            if t in tables:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"  Table '{t}' present (rows: {count})")
            else:
                print(f"  ERROR: Table '{t}' MISSING!")
                sys.exit(1)

        # Verify incident columns
        incident_cols = {col["name"] for col in inspector.get_columns("incident")}
        expected_cols = [
            "pinned_policy_bundle_id",
            "policy_release_id",
            "canary_arm",
            "assignment_hash_val",
        ]
        for ec in expected_cols:
            if ec in incident_cols:
                print(f"  Incident column '{ec}' present")
            else:
                print(f"  ERROR: Incident column '{ec}' MISSING!")
                sys.exit(1)

        # Verify historical tables
        historical_tables = [
            "tenant",
            "incident",
            "approval_request",
            "experience_capsule",
            "operational_lesson",
            "candidate_evaluation_manifest",
        ]
        for ht in historical_tables:
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM {ht}")).scalar()
            print(f"  Historical table '{ht}' rows: {cnt}")

    engine.dispose()
    print(f"Migration on {db_name} successful!")


if __name__ == "__main__":
    migrate_database("opsblaze_test")
    migrate_database("opsblaze")
