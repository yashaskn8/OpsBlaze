"""Fail CI if a new /v1 endpoint forgets tenant or mutation permissions.

This structural gate complements behavioral token and cross-tenant tests.
"""

from fastapi.routing import APIRoute
from starlette.routing import Route

from backend.api import app
from backend.auth import (
    get_principal, local_operator, local_reviewer,
    local_security_admin, local_tenant,
)


PUBLIC_GETS = frozenset({"/v1/health/ready"})
# This write endpoint authenticates a separately-audienced signed IdP JWT in
# the handler, not a human API bearer token or a development shared secret.
SIGNED_EVENT_POSTS = frozenset({"/v1/security/idp-events"})


def test_route_inventory_cannot_silently_skip_nested_routes():
    # An opaque included router can contain /v1 endpoints that the flat
    # dependency audit never sees. Fail closed if registration changes.
    builtin_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for route in app.routes:
        assert isinstance(route, APIRoute) or (
            type(route) is Route and route.path in builtin_paths
        ), (type(route), getattr(route, "path", None))


def test_api_authorization_matrix_is_exhaustive():
    protected = [
        route for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/v1/")
    ]
    assert protected
    for route in protected:
        direct = {dependency.call for dependency in route.dependant.dependencies}
        methods = route.methods or set()
        if methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            if route.path in SIGNED_EVENT_POSTS:
                assert route.methods == {"POST"}
                assert local_tenant not in direct
                continue
            assert local_tenant in direct, (route.path, methods)
            assert (
                local_operator in direct or local_reviewer in direct
                or local_security_admin in direct
            ), (route.path, methods)
        elif route.path not in PUBLIC_GETS:
            assert (
                local_tenant in direct or get_principal in direct
            ), (route.path, methods)


def test_identity_and_health_are_the_only_intentional_exceptions():
    unauthenticated = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/v1/"):
            continue
        direct = {dependency.call for dependency in route.dependant.dependencies}
        if local_tenant not in direct and get_principal not in direct:
            unauthenticated.append(route.path)
    assert set(unauthenticated) == {"/v1/health/ready", "/v1/security/idp-events"}
