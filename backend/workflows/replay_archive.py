"""Version-bound synthetic Temporal history archives; no live worker or RPC."""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from backend.workflows.replay_completion import validate_completion

CASES = ("approve", "reject", "cancel")
MAX_HISTORY_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024


def _read_bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("archive entry must be a regular local file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("archive entry size outside supported bounds")
    return data


def _validate_identity(case: str, workflow_id: str) -> None:
    if not isinstance(workflow_id, str) or not re.fullmatch(
        rf"replay-{case}-[0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}}",
        workflow_id,
    ):
        raise ValueError("unexpected synthetic workflow identity")


def _validate_commit(commit: str) -> None:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("archive source commit must be a full git SHA")


def write_archive(
    directory: Path, source_commit: str, histories: dict[str, tuple[str, str]],
) -> None:
    """Write only the three known synthetic scenarios, to a new directory."""
    _validate_commit(source_commit)
    if set(histories) != set(CASES):
        raise ValueError("incomplete synthetic replay archive")
    entries = {}
    payloads = {}
    for case in CASES:
        workflow_id, raw = histories[case]
        _validate_identity(case, workflow_id)
        data = raw.encode("utf-8")
        if not data or len(data) > MAX_HISTORY_BYTES:
            raise ValueError("history size outside supported bounds")
        payloads[case] = data
        entries[case] = {
            "workflow_id": workflow_id, "sha256": hashlib.sha256(data).hexdigest(),
        }
    directory.mkdir(parents=True, exist_ok=False)
    for case, data in payloads.items():
        (directory / f"{case}.json").write_bytes(data)
    manifest = {"schema_version": 1, "source_commit": source_commit, "histories": entries}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def load_archive(directory: Path, expected_source_commit: str) -> list[tuple[str, str]]:
    """Check the exact source version, fixed names, identities and byte digests."""
    _validate_commit(expected_source_commit)
    manifest = json.loads(_read_bounded(directory / "manifest.json", MAX_MANIFEST_BYTES))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "source_commit", "histories"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["source_commit"] != expected_source_commit
        or not isinstance(manifest["histories"], dict)
        or set(manifest["histories"]) != set(CASES)
    ):
        raise ValueError("invalid or mismatched archive manifest")
    histories = []
    for case in CASES:
        entry = manifest["histories"][case]
        if not isinstance(entry, dict) or set(entry) != {"workflow_id", "sha256"}:
            raise ValueError("invalid history manifest entry")
        workflow_id = entry["workflow_id"]
        _validate_identity(case, workflow_id)
        raw = _read_bounded(directory / f"{case}.json", MAX_HISTORY_BYTES)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("history digest mismatch")
        histories.append((workflow_id, raw.decode("utf-8")))
    return histories


async def replay_archive(directory: Path, expected_source_commit: str) -> None:
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer

    from backend.workflows.incident import IncidentWorkflow

    histories = load_archive(directory, expected_source_commit)
    replayer = Replayer(workflows=[IncidentWorkflow])
    for workflow_id, raw in histories:
        history = WorkflowHistory.from_json(workflow_id, raw)
        validate_completion(workflow_id, history.events)
        await replayer.replay_workflow(history)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    asyncio.run(replay_archive(args.directory, args.source_commit))
    print("Synthetic archive replay passed; not production recovery certification.")


if __name__ == "__main__":
    main()
