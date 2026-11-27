"""Release scope shared by the API and the artifact manifest."""

import argparse
import json
import re
from typing import Literal

ReleaseProfile = Literal["community"]
COMMUNITY_PROFILE: ReleaseProfile = "community"


def release_profile() -> dict[str, str | bool]:
    return {
        "architecture_version": "2.0",
        "profile": COMMUNITY_PROFILE,
        "effect_scope": "simulator_only",
        "live_policy_promotion": False,
        "enterprise_acceptance": "ENTERPRISE_BLOCKED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_commit")
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.source_commit) is None:
        parser.error("source commit must be a full lowercase Git SHA")
    print(json.dumps({**release_profile(), "source_commit": args.source_commit}, indent=2))


if __name__ == "__main__":
    main()
