"""Operations router must retain HTTP methods, dependencies and response status."""

from fastapi.routing import APIRoute

from backend.api import app
from backend.auth import local_operator, local_security_admin, local_tenant
from backend.database import get_session
from backend.routers.operations import router
from backend.settings import get_settings


def test_operations_routes_are_registered_once_with_original_contract():
    expected = {
        ("/v1/health/ready", "GET"): ({get_session, get_settings}, None),
        ("/v1/operations/queues", "GET"): (
            {local_tenant, get_session, get_settings}, None,
        ),
        ("/v1/operations/dead-letters", "GET"): (
            {local_tenant, get_session}, None,
        ),
        ("/v1/operations/dead-letters/{entry_id}/acknowledge", "POST"): (
            {local_tenant, get_session, local_operator}, 202,
        ),
        ("/v1/operations/dead-letters/{entry_id}/redrive", "POST"): (
            {local_tenant, get_session, local_security_admin}, 202,
        ),
    }
    extracted = {
        (route.path, method): route
        for route in router.routes if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert set(extracted) == set(expected)
    for (path, method), (dependencies, status) in expected.items():
        matches = [
            route for route in app.routes
            if isinstance(route, APIRoute)
            and route.path == path and method in route.methods
        ]
        assert len(matches) == 1
        route = matches[0]
        assert route.endpoint is extracted[path, method].endpoint
        assert route.status_code == status
        assert {dep.call for dep in route.dependant.dependencies} == dependencies
        assert route.endpoint.__module__ == "backend.routers.operations"
