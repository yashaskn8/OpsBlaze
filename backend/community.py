"""Serve the Community workbench and API together on IPv4 loopback."""

import argparse
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import app
from backend.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dir", type=Path, default=Path("frontend/dist"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not (args.frontend_dir / "index.html").is_file():
        parser.error("the frontend build is missing index.html")
    try:
        get_settings()
    except ValueError as exc:
        parser.error(str(exc))

    async def oidc_callback(_request: Request) -> FileResponse:
        return FileResponse(args.frontend_dir / "index.html", media_type="text/html")

    app.add_route("/auth/callback", oidc_callback, methods=["GET"], include_in_schema=False)
    app.mount("/", StaticFiles(directory=args.frontend_dir, html=True), name="workbench")
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
