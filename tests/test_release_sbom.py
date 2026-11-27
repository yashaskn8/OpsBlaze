import json
from pathlib import Path

import pytest

from scripts.release_sbom import make_sbom, python_components


def test_python_inventory_retains_platform_specific_pins():
    components = python_components(
        "# runtime lock\n"
        "example_pkg==1.2 ; sys_platform == 'win32' \\\n"
        "  --hash=sha256:example\n"
        "example_pkg==1.3 ; sys_platform != 'win32' \\\n"
        "  --hash=sha256:example\n"
    )
    assert [row["purl"] for row in components] == ["pkg:pypi/example-pkg@1.2", "pkg:pypi/example-pkg@1.3"]
    assert all(row["properties"][0]["name"] == "opsblaze:pypi:environment-marker" for row in components)
    with pytest.raises(ValueError, match="pinned"):
        python_components("example_pkg>=1.2")


def test_release_inventory_rejects_a_missing_npm_runtime_package():
    root = Path(__file__).resolve().parents[1]
    packages = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))["packages"]
    npm_components = [
        {"name": path.rsplit("node_modules/", 1)[-1], "version": row["version"], "bom-ref": path}
        for path, row in packages.items()
        if path and not row.get("dev", False)
    ]
    context = {"bomFormat": "CycloneDX", "metadata": {"component": {"bom-ref": "frontend"}}}
    with pytest.raises(ValueError, match="npm SBOM disagrees"):
        make_sbom(root, {**context, "components": npm_components[:-1]})
    bom = make_sbom(root, {**context, "components": npm_components})
    assert bom["specVersion"] == "1.6"
    assert set(row["name"] for row in bom["metadata"]["properties"]) == {
        "opsblaze:pypi:runtime-lock-sha256", "opsblaze:npm:lock-sha256",
    }
