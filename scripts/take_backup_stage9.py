import os
import subprocess
import sys

sys.path.insert(0, ".")
from backend.config import get_settings


def take_backup(db_name: str, out_file: str) -> None:
    settings = get_settings()
    env = os.environ.copy()
    env["PGPASSWORD"] = settings.postgres_password
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    pg_dump = r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe"
    cmd = [
        pg_dump,
        "-h", settings.postgres_host,
        "-p", str(settings.postgres_port),
        "-U", settings.postgres_user,
        "-d", db_name,
        "-F", "c",
        "-f", out_file
    ]
    print(f"Backing up {db_name} to {out_file}...")
    res = subprocess.run(cmd, check=False, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error backing up {db_name}: {res.stderr}")
        sys.exit(1)
    size = os.path.getsize(out_file)
    print(f"Backup successful: {size} bytes")

    # Verify with pg_restore -l
    pg_restore = r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe"
    toc_res = subprocess.run([pg_restore, "-l", out_file], check=False, capture_output=True, text=True)
    if toc_res.returncode != 0:
        print(f"TOC verification failed: {toc_res.stderr}")
        sys.exit(1)
    print(f"TOC verified: {len(toc_res.stdout.splitlines())} items in archive.")

def main() -> None:
    take_backup("opsblaze", ".local/backups/stage9/opsblaze_pre_stage9.dump")
    take_backup("opsblaze_test", ".local/backups/stage9/opsblaze_test_pre_stage9.dump")

if __name__ == "__main__":
    main()
