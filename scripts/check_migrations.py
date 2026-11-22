"""Fail fast on malformed or ambiguous Alembic history."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def main() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise SystemExit(f"expected exactly one Alembic head: {heads}")
    revisions = list(scripts.walk_revisions())
    files = list(Path("migrations/versions").glob("*.py"))
    if not revisions or len(revisions) != len(files):
        raise SystemExit("migrations missing or unrecognized by Alembic")
    if any(len(item.revision) > 32 for item in revisions):
        raise SystemExit("Alembic revision exceeds varchar(32)")
    if len({item.revision for item in revisions}) != len(revisions):
        raise SystemExit("duplicate Alembic revision")
    print(f"Verified {len(revisions)} migrations; head: {heads[0]}")


if __name__ == "__main__":
    main()
