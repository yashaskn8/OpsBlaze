"""Derive the dual-profile acceptance ledger from the original atomic audit."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

COMMUNITY_GATES = ("C1", "C2", "C3", "C4", "C5", "C6")


def release_requirements(docs: Path, source_commit: str | None = None) -> dict:
    audit = json.loads((docs / "requirements-audit.json").read_text(encoding="utf-8"))
    ledger = json.loads((docs / "requirements-ledger.json").read_text(encoding="utf-8"))
    profiles = ledger["release_profiles"]
    community_config = profiles["community"]
    outside = community_config["outside_local_scope"]
    accepted = community_config["accepted_evidence"]
    gates = community_config["gate_evidence"]
    capability_gates = {row["id"]: row["production_gates"] for row in ledger["requirements"]}
    rows = []
    for capability in audit["capabilities"]:
        for row in capability["requirements"]:
            item_id = row["id"]
            historical = row["status"]
            local_implementation = row.get("local_implementation", "")
            local_verification = row.get("local_verification", "")
            if historical == "VERIFIED WORKING" or local_implementation.startswith("COMPLETE_"):
                implementation = "IMPLEMENTED"
            else:
                implementation = "PARTIALLY_IMPLEMENTED"
            if "EXACT_CI_PASS" in local_verification:
                verification = "CI_VERIFIED"
            elif historical == "VERIFIED WORKING" or "PASS" in local_verification or local_verification.startswith("VERIFIED"):
                verification = "LOCAL_VERIFIED"
            else:
                verification = "IMPLEMENTED_UNVERIFIED"

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
    qualified = pending == 0 and gates.keys() == set(COMMUNITY_GATES)
    return {
        "architecture_version": profiles["architecture_version"],
        "source_commit": source_commit,
        "evidence_baseline": profiles["evidence_baseline"],
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
    args = parser.parse_args()
    if args.source_commit is not None and re.fullmatch(r"[0-9a-f]{40}", args.source_commit) is None:
        parser.error("source commit must be a full lowercase Git SHA")
    report = release_requirements(args.docs, args.source_commit)
    if args.summary:
        del report["requirements"]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
