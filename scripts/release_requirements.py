"""Report the OpsBlaze Omega inventory and its historical blueprint traceability."""

import argparse
from collections import Counter
from hashlib import file_digest
import json
from pathlib import Path
import re
import tarfile

COMMUNITY_GATES = ("C1", "C2", "C3", "C4", "C5", "C6")
ENTERPRISE_GATES = frozenset(f"G{i}" for i in range(1, 7))
PROFILES = ("community", "enterprise")
APPLICABILITY = {"MANDATORY", "CONDITIONAL", "RECOMMENDED", "NOT_APPLICABLE"}
VERIFICATION_STATES = {
    "NOT_ASSESSED", "NOT_IMPLEMENTED", "PARTIALLY_IMPLEMENTED", "IMPLEMENTED_UNVERIFIED",
    "LOCALLY_VERIFIED", "CI_VERIFIED", "ACCEPTANCE_PENDING", "BLOCKED_EXTERNAL_DEPENDENCY",
    "INCONCLUSIVE", "FAILED", "NOT_APPLICABLE", "ACCEPTED_FOR_DECLARED_SCOPE",
}
OMEGA_EXCLUSIONS = {"SH-REL-01": "USER_AUTHORIZED_SCOPE_EXCLUSION"}
IMPORTED_STATES = VERIFICATION_STATES - {"ACCEPTED_FOR_DECLARED_SCOPE"}
VERIFICATION_PRECEDENCE = (
    "FAILED", "BLOCKED_EXTERNAL_DEPENDENCY", "NOT_IMPLEMENTED", "NOT_ASSESSED",
    "PARTIALLY_IMPLEMENTED", "INCONCLUSIVE", "ACCEPTANCE_PENDING", "IMPLEMENTED_UNVERIFIED",
    "LOCALLY_VERIFIED", "CI_VERIFIED",
)


def omega_requirements(path: Path, anchor_ids: set[str], capability_ids: set[str]) -> dict:
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger["schema"] != "titan20-omega-ledger/1" or ledger["project"] != "OpsBlaze":
        raise ValueError("unsupported OpsBlaze Omega ledger")
    rows = ledger["requirements"]
    by_id = {row["id"]: row for row in rows}
    anchors = ledger["traceability"]
    children = [child for group in anchors.values() for child in group]
    original_ids = {row["id"] for row in rows if row["origin"] == "ORIGINAL"}
    if (
        len(by_id) != len(rows)
        or anchors.keys() != anchor_ids
        or any(not group for group in anchors.values())
        or len(children) != len(original_ids)
        or set(children) != original_ids
    ):
        raise ValueError("Omega rows must preserve unique IDs and cover each original anchor")
    for anchor_id, group in anchors.items():
        expected_parent = f"OB-{anchor_id}" if len(group) > 1 else None
        for child in group:
            if by_id[child]["original_id"] != anchor_id or by_id[child]["parent_id"] != expected_parent:
                raise ValueError(f"Omega traceability mismatch for {child}")
    exclusions = ledger["exclusions"]
    manual = ledger["manual_acceptance"]
    if exclusions != OMEGA_EXCLUSIONS or not (exclusions.keys() | manual.keys()) <= by_id.keys():
        raise ValueError("Omega ledger contains an unrecognized scope exclusion or manual requirement")
    for row in rows:
        row_id = row["id"]
        dependencies = set(row["dependencies"])
        applicability = row["applicability"]
        status = row["profile_status"]
        per_project = row["status"] == "PER_PROJECT"
        expected_status_keys = {"OpsBlaze"} if per_project else set(PROFILES)
        if (
            row["project"] != "OpsBlaze"
            or not row["acceptance_criterion"]
            or not row["evidence_required"]["positive"]
            or not row["evidence_required"]["negative"]
            or not dependencies <= capability_ids | ENTERPRISE_GATES
            or applicability.keys() != set(PROFILES)
            or not set(applicability.values()) <= APPLICABILITY
            or status.keys() != expected_status_keys
            or not set(status.values()) <= IMPORTED_STATES
            or (not per_project and row["status"] not in IMPORTED_STATES)
        ):
            raise ValueError(f"invalid Omega applicability, evidence, status or dependency for {row_id}")
        community = status["OpsBlaze"] if per_project else status["community"]
        enterprise = status.get("enterprise", "NOT_ASSESSED")
        if applicability["community"] == "NOT_APPLICABLE":
            if community == "BLOCKED_EXTERNAL_DEPENDENCY":
                enterprise = community
            community = "NOT_APPLICABLE"
        elif row_id in manual:
            community = "ACCEPTANCE_PENDING"
        row["verification"] = {"community": community, "enterprise": enterprise}
        row["profile_dependencies"] = {
            "community": [
                dep for dep in row["dependencies"]
                if dep in capability_ids and applicability["community"] != "NOT_APPLICABLE"
            ],
            "enterprise": row["dependencies"],
        }

    in_scope = [row for row in rows if row["id"] not in exclusions]
    verification = {
        profile: Counter(row["verification"][profile] for row in in_scope)
        for profile in PROFILES
    }
    return {
        "source": ledger["source"],
        "total": len(rows),
        "origins": dict(Counter(row["origin"] for row in rows)),
        "original_anchors": anchors,
        "exclusions": exclusions,
        "manual_acceptance": manual,
        **{
            profile: {
                "applicability": dict(Counter(row["applicability"][profile] for row in in_scope)),
                "verification": dict(verification[profile]),
                "accepted": verification[profile]["ACCEPTED_FOR_DECLARED_SCOPE"],
            }
            for profile in PROFILES
        },
        "requirements": rows,
    }


def release_requirements(docs: Path, source_commit: str | None = None) -> dict:
    audit = json.loads((docs / "requirements-audit.json").read_text(encoding="utf-8"))
    ledger = json.loads((docs / "requirements-ledger.json").read_text(encoding="utf-8"))
    profiles = ledger["release_profiles"]
    community_config = profiles["community"]
    outside = community_config["outside_local_scope"]
    accepted = community_config["accepted_evidence"]
    gates = community_config["gate_evidence"]
    capability_gates = {row["id"]: row["production_gates"] for row in ledger["requirements"]}
    anchor_ids = {row["id"] for capability in audit["capabilities"] for row in capability["requirements"]}
    omega = omega_requirements(docs / ledger["omega_atomic_ledger"], anchor_ids, set(capability_gates))
    atomic_status = {row["id"]: row["verification"]["community"] for row in omega["requirements"]}
    rows = []
    for capability in audit["capabilities"]:
        for row in capability["requirements"]:
            item_id = row["id"]
            historical = row["status"]
            local_implementation = row.get("local_implementation", "")
            if historical == "VERIFIED WORKING" or local_implementation.startswith("COMPLETE_"):
                implementation = "IMPLEMENTED"
            else:
                implementation = "PARTIALLY_IMPLEMENTED"
            child_status = {atomic_status[child] for child in omega["original_anchors"][item_id]}
            verification = next(
                (state for state in VERIFICATION_PRECEDENCE if state in child_status),
                "NOT_APPLICABLE",
            )

            community_status = verification if implementation == "IMPLEMENTED" else implementation
            if historical == "PARTIALLY IMPLEMENTED":
                community_status = "PARTIALLY_IMPLEMENTED"
            if item_id in outside:
                community_status = "NOT_APPLICABLE_TO_COMMUNITY"
            elif item_id in accepted:
                community_status = "COMMUNITY_ACCEPTED"

            rows.append({
                "id": item_id,
                "blueprint_section": capability["section"],
                "blueprint_pages": capability["pdf_pages"],
                "requirement": row["requirement"],
                "historical_status": historical,
                "engineering_status": implementation,
                "runtime_verification": verification,
                "implementation": row["implementation"],
                "consumers": row["consumers"],
                "tests": row["tests"],
                "evidence": row["evidence"],
                "dependencies": row["dependencies"],
                "remaining": row["remaining"],
                "community": {
                    "applicable": item_id not in outside,
                    "status": community_status,
                    "reason": outside.get(item_id, ""),
                    "acceptance_evidence": accepted.get(item_id),
                },
                "enterprise": {
                    "applicable": True,
                    "status": "ENTERPRISE_BLOCKED",
                    "gates": row.get("external_acceptance", {}).get("gates") or capability_gates[capability["capability"]],
                },
            })

    row_ids = {row["id"] for row in rows}
    if len(row_ids) != len(rows) or not (outside.keys() | accepted.keys()) <= row_ids or outside.keys() & accepted.keys():
        raise ValueError("profile applicability or acceptance refers to inconsistent requirement IDs")
    if not gates.keys() <= set(COMMUNITY_GATES) or any(
        not isinstance(evidence, str) or not evidence.strip()
        for evidence in (*accepted.values(), *gates.values())
    ):
        raise ValueError("each Community acceptance needs explicit evidence against a known requirement or gate")
    historical_counts = Counter(row["historical_status"] for row in rows)
    if dict(historical_counts) != audit["status_counts"]:
        raise ValueError("historical audit totals disagree with the atomic requirements")
    counts = Counter(row["community"]["status"] for row in rows)
    applicable = sum(row["community"]["applicable"] for row in rows)
    pending = applicable - counts["COMMUNITY_ACCEPTED"]
    omega_community = omega["community"]
    qualified = (
        pending == 0
        and gates.keys() == set(COMMUNITY_GATES)
        and omega_community["accepted"] == omega_community["applicability"]["MANDATORY"]
    )
    return {
        "architecture_version": profiles["architecture_version"],
        "source_commit": source_commit,
        "evidence_baseline": profiles["evidence_baseline"],
        "omega": omega,
        "historical_status_counts": dict(historical_counts),
        "community": {
            "applicable": applicable,
            "accepted": counts["COMMUNITY_ACCEPTED"],
            "pending": pending,
            "not_applicable": counts["NOT_APPLICABLE_TO_COMMUNITY"],
            "release_status": "COMMUNITY_ACCEPTED" if qualified else "PENDING",
            "gates": {
                gate: {"status": "COMMUNITY_ACCEPTED" if gate in gates else "PENDING", "evidence": gates.get(gate)}
                for gate in COMMUNITY_GATES
            },
            "acceptance_source": community_config["acceptance_source"],
        },
        "enterprise": {
            "applicable": len(rows),
            "blocked": len(rows),
            "release_status": "ENTERPRISE_BLOCKED",
            "acceptance_source": profiles["enterprise"]["acceptance_source"],
        },
        "requirements": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--decision", action="store_true", help="emit an artifact-bound release decision")
    parser.add_argument("--artifact", type=Path, help="completed artifact to hash for a release decision")
    args = parser.parse_args()
    if args.source_commit is not None and re.fullmatch(r"[0-9a-f]{40}", args.source_commit) is None:
        parser.error("source commit must be a full lowercase Git SHA")
    if args.decision and (not args.source_commit or args.artifact is None):
        parser.error("a release decision requires --source-commit and --artifact")
    if args.artifact is not None and not args.decision:
        parser.error("--artifact requires --decision")
    report = release_requirements(args.docs, args.source_commit)
    report["report_kind"] = "requirements_inventory"
    if args.decision:
        if not args.artifact.is_file():
            parser.error("the release artifact must be a readable file")
        try:
            with args.artifact.open("rb") as file:
                digest = file_digest(file, "sha256").hexdigest()
                file.seek(0)
                with tarfile.open(fileobj=file, mode="r:gz") as archive:
                    markers = [
                        member for member in archive
                        if member.name in ("SOURCE_COMMIT", "./SOURCE_COMMIT")
                    ]
                    expected = f"{args.source_commit}\n".encode("ascii")
                    if len(markers) != 1 or not markers[0].isfile() or markers[0].size != len(expected):
                        parser.error("the release artifact must contain one valid SOURCE_COMMIT file")
                    with archive.extractfile(markers[0]) as marker:
                        if marker.read() != expected:
                            parser.error("the release artifact source commit does not match --source-commit")
        except (OSError, EOFError, tarfile.TarError) as exc:
            parser.error(f"could not read the release artifact: {exc}")
        report["report_kind"] = "release_decision"
        report["artifact_sha256"] = digest
    if args.summary or args.decision:
        del report["requirements"]
        del report["omega"]["requirements"]
        del report["omega"]["original_anchors"]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
