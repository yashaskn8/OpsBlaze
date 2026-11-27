"""FastAPI documentation pages with narrowly scoped browser permissions."""

from base64 import b64encode
from hashlib import sha256
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

CONTENT_POLICY = (
    "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
SWAGGER_ASSETS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"
REDOC_SCRIPT = "https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"
REDOC_LOGO = "https://cdn.redoc.ly/redoc/logo-mini.svg"
DOCS_FAVICON = "https://fastapi.tiangolo.com/img/favicon.png"
OAUTH_REDIRECT = "/docs/oauth2-redirect"


def browser_content_policy(issuer: str) -> str:
    if not issuer:
        return CONTENT_POLICY
    try:
        url = urlsplit(issuer)
        host = url.hostname or ""
        port = url.port
    except ValueError:
        return CONTENT_POLICY
    if url.scheme != "https" or url.username or url.password:
        return CONTENT_POLICY
    if re.fullmatch(r"[0-9a-f:]+", host) and ":" in host:
        host = f"[{host}]"
    elif not re.fullmatch(r"[a-z0-9.-]+", host):
        return CONTENT_POLICY
    origin = f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"
    return f"{CONTENT_POLICY}; connect-src 'self' {origin}"


def doc_content_policy(response: HTMLResponse, script: str = "", stylesheet: str = "") -> HTMLResponse:
    # Only hash the inline code emitted by the trusted FastAPI documentation templates.
    hashes = [
        f"'sha256-{b64encode(sha256(body).digest()).decode('ascii')}'"
        for body in re.findall(rb"<script>(.*?)</script>", response.body, flags=re.S)
    ]
    scripts = " ".join(source for source in ("'self'", script, *hashes) if source)
    styles = " ".join(source for source in ("'self'", "'unsafe-inline'", stylesheet) if source)
    redoc = script == REDOC_SCRIPT
    images = f"{DOCS_FAVICON} {REDOC_LOGO}" if redoc else DOCS_FAVICON
    response.headers["Content-Security-Policy"] = (
        f"{CONTENT_POLICY}; script-src {scripts}; style-src {styles}; "
        f"img-src 'self' data: {images}"
    )
    if redoc:
        response.headers["Content-Security-Policy"] += "; worker-src blob:"
    return response


def register_docs(app: FastAPI) -> None:
    async def swagger(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        script = f"{SWAGGER_ASSETS}/swagger-ui-bundle.js"
        stylesheet = f"{SWAGGER_ASSETS}/swagger-ui.css"
        response = get_swagger_ui_html(
            openapi_url=root + app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=root + OAUTH_REDIRECT,
            swagger_js_url=script,
            swagger_css_url=stylesheet,
            swagger_favicon_url=DOCS_FAVICON,
        )
        return doc_content_policy(response, script, stylesheet)

    async def oauth_redirect(_request: Request) -> HTMLResponse:
        return doc_content_policy(get_swagger_ui_oauth2_redirect_html())

    async def redoc(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        return doc_content_policy(get_redoc_html(
            openapi_url=root + app.openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_js_url=REDOC_SCRIPT,
            redoc_favicon_url=DOCS_FAVICON,
            with_google_fonts=False,
        ), REDOC_SCRIPT)

    app.add_route("/docs", swagger, methods=["GET"], include_in_schema=False)
    app.add_route(OAUTH_REDIRECT, oauth_redirect, methods=["GET"], include_in_schema=False)
    app.add_route("/redoc", redoc, methods=["GET"], include_in_schema=False)
