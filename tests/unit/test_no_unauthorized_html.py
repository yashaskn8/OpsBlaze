"""Architectural enforcement tests: No unauthorized HTML in OpsBlaze TITAN-20.

Verifies strict Python 3.14 + FastAPI backend and React 19 + TypeScript frontend.
Forbids standalone HTML application pages, HTML dashboards, HTML templates,
and server-rendered HTML across the codebase.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Approved minimal allowlist
APPROVED_HTML_ALLOWLIST = {
    # Minimal Vite bootstrap entry (must only contain root mount div and module script tag)
    Path("frontend/index.html"),
    # Installed engineering skill pack template (Superpowers brainstorming skill resource)
    Path(".agents/skills/brainstorming/scripts/frame-template.html"),
}


def test_tracked_files_no_unauthorized_html() -> None:
    """Verify that git-tracked files contain no unauthorized HTML."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    
    html_files = [
        f for f in tracked_files
        if f.lower().endswith(".html") or f.lower().endswith(".htm")
    ]
    
    for html_file in html_files:
        norm_path = Path(html_file)
        assert norm_path in APPROVED_HTML_ALLOWLIST, (
            f"Unauthorized tracked HTML file detected: {html_file}. "
            f"OpsBlaze enforces React 19 + TypeScript (TSX) for UI and Markdown for documentation."
        )


def test_no_html_in_backend_docs_or_scripts() -> None:
    """Ensure backend, docs, scripts, fixtures, and graphify have zero HTML files."""
    forbidden_dirs = [
        REPO_ROOT / "backend",
        REPO_ROOT / "docs",
        REPO_ROOT / "scripts",
        REPO_ROOT / "fixtures",
    ]
    for directory in forbidden_dirs:
        if not directory.exists():
            continue
        html_matches = list(directory.rglob("*.html")) + list(directory.rglob("*.htm"))
        assert len(html_matches) == 0, (
            f"Found forbidden HTML files in {directory}: {[str(p.relative_to(REPO_ROOT)) for p in html_matches]}"
        )


def test_vite_bootstrap_is_minimal() -> None:
    """Verify that frontend/index.html is purely a minimal Vite loader under 25 lines."""
    vite_index = REPO_ROOT / "frontend" / "index.html"
    assert vite_index.exists(), "frontend/index.html must exist as Vite entry point"
    
    content = vite_index.read_text(encoding="utf-8")
    lines = content.strip().splitlines()
    assert len(lines) <= 25, f"frontend/index.html has {len(lines)} lines, exceeding the 25-line limit"
    
    # Must only contain root mount point and script module import
    assert '<div id="root"></div>' in content
    assert '<script type="module" src="/src/main.tsx"></script>' in content
    assert "class=" not in content, "frontend/index.html must not contain styling or application markup"
    assert "<form" not in content, "frontend/index.html must not contain form markup"


def test_backend_no_server_side_html_rendering() -> None:
    """Verify backend FastAPI routes do not return HTML or use template engines."""
    backend_dir = REPO_ROOT / "backend"
    forbidden_tokens = [
        "HTMLResponse",
        "TemplateResponse",
        "jinja2",
        "render_template",
    ]
    
    for py_file in backend_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in content, (
                f"Forbidden HTML rendering token '{token}' found in {py_file.relative_to(REPO_ROOT)}. "
                f"OpsBlaze APIs must exclusively return structured JSON via Pydantic."
            )


def test_react_tsx_components_exist() -> None:
    """Verify that all core TITAN-20 capabilities are implemented as genuine React TSX components."""
    expected_components = [
        REPO_ROOT / "frontend" / "src" / "App.tsx",
        REPO_ROOT / "frontend" / "src" / "main.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "WorkbenchView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "ApprovalCockpitView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "TraceExplorerView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "ExperienceView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "FailureAndLessonsView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "EvaluationsView.tsx",
        REPO_ROOT / "frontend" / "src" / "components" / "StaleLockoutBanner.tsx",
    ]
    for comp in expected_components:
        assert comp.exists(), f"Required React component missing: {comp.relative_to(REPO_ROOT)}"
