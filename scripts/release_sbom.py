"""Create the release's CycloneDX inventory from the runtime lock inputs."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def python_components(lock: str) -> list[dict]:
    components: dict[tuple[str, str], dict] = {}
    for line in lock.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        requirement = Requirement(line.removesuffix("\\").strip())
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise ValueError("Python SBOM requires pinned runtime dependencies")
        name, version = canonicalize_name(requirement.name), specifiers[0].version
        purl = f"pkg:pypi/{name}@{version}"
        component = components.setdefault((name, version), {
            "type": "library", "name": name, "version": version, "purl": purl, "bom-ref": purl,
        })
        if requirement.marker:
            component.setdefault("properties", []).append({
                "name": "opsblaze:pypi:environment-marker", "value": str(requirement.marker),
            })
    if not components:
        raise ValueError("Python runtime lock is empty")
    return [components[key] for key in sorted(components)]


def make_sbom(root: Path, npm_inventory: dict) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    python_lock = (root / "requirements-runtime.lock").read_bytes()
    npm_lock = (root / "frontend/package-lock.json").read_bytes()
    if npm_inventory["bomFormat"] != "CycloneDX":
        raise ValueError("expected CycloneDX npm inventory")
    expected_npm = {
        (pkg.get("name", path.rsplit("node_modules/", 1)[-1]), pkg["version"])
        for path, pkg in json.loads(npm_lock)["packages"].items()
        if path and not pkg.get("dev", False)
    }
    npm_components = npm_inventory["components"]
    if expected_npm != {(pkg["name"], pkg["version"]) for pkg in npm_components}:
        raise ValueError("npm SBOM disagrees with the runtime package lock")

    purl = f"pkg:pypi/{canonicalize_name(project['name'])}@{project['version']}"
    components = [
        *python_components(python_lock.decode("utf-8")),
        npm_inventory["metadata"]["component"], *npm_components,
    ]
    if len({component["bom-ref"] for component in components}) != len(components):
        raise ValueError("SBOM component references must be unique")
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {
            "component": {
                "type": "application", "name": project["name"], "version": project["version"],
                "purl": purl, "bom-ref": purl,
            },
            "properties": [
                {"name": "opsblaze:pypi:runtime-lock-sha256", "value": sha256(python_lock).hexdigest()},
                {"name": "opsblaze:npm:lock-sha256", "value": sha256(npm_lock).hexdigest()},
            ],
        },
        "components": components,
        "dependencies": npm_inventory.get("dependencies", []),
    }


def main() -> None:
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npm-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.npm_inventory.read_text(encoding="utf-8-sig"))
    output = json.dumps(make_sbom(Path.cwd(), inventory), indent=2) + "\n"
    error = JsonStrictValidator(SchemaVersion.V1_6).validate_str(output)
    if error is not None:
        parser.error(f"generated CycloneDX 1.6 SBOM is invalid: {error}")
    args.output.write_text(output, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
