"""Conservative high-confidence tracked secret check, not a full scanner."""
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})"),
    "AWS access ID": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "OpenAI key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"),
}


def main() -> None:
    names = [x.decode("utf-8") for x in subprocess.check_output(
        ["git", "ls-files", "-z"]).split(b"\0") if x]
    failures = []
    for name in names:
        path = Path(name)
        filename = path.name.casefold()
        if (filename.startswith(".env") or filename.endswith(".credentials.env")) and name != ".env.example":
            failures.append(f"{name}: environment file must not be tracked")
        if path.suffix in {".pem", ".key", ".p12", ".pfx"}:
            failures.append(f"{name}: credential file must not be tracked")
        if path.is_file():
            contents = path.read_bytes()
            for label, pattern in PATTERNS.items():
                if pattern.search(contents):
                    failures.append(f"{name}: possible {label}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Checked {len(names)} tracked paths")


if __name__ == "__main__":
    main()
