"""Fail CI if a new /v1 endpoint forgets tenant or mutation permissions.

This structural gate complements behavioral token and cross-tenant tests.
"""

from fastapi.routing import APIRoute

from backend.api import app
from backend.auth import (
    get_principal, local_operator, local_reviewer,
    local_security_admin, local_tenant,
)


PUBLIC_GETS = frozenset({"/v1/health/ready"})


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
    assert unauthenticated == ["/v1/health/ready"]
