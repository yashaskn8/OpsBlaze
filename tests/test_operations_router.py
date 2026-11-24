"""Operations router must retain HTTP methods, dependencies and response status."""

from fastapi.routing import APIRoute

from backend.api import app
from backend.auth import local_operator, local_security_admin, local_tenant
from backend.database import get_session
from backend.routers.operations import (
    acknowledge_experience_dead_letter, experience_dead_letters, readiness,
    redrive_experience_dead_letter, tenant_queues,
)
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
    endpoints = {
        ("/v1/health/ready", "GET"): readiness,
        ("/v1/operations/queues", "GET"): tenant_queues,
        ("/v1/operations/dead-letters", "GET"): experience_dead_letters,
        ("/v1/operations/dead-letters/{entry_id}/acknowledge", "POST"):
            acknowledge_experience_dead_letter,
        ("/v1/operations/dead-letters/{entry_id}/redrive", "POST"):
            redrive_experience_dead_letter,
    }
    registered = [
        (route.path, method)
        for route in app.routes if isinstance(route, APIRoute)
        and route.endpoint.__module__ == "backend.routers.operations"
        for method in route.methods
    ]
    assert len(registered) == len(expected)
    assert set(registered) == set(expected) == set(endpoints)
    for (path, method), (dependencies, status) in expected.items():
        matches = [
            route for route in app.routes
            if isinstance(route, APIRoute)
            and route.path == path and method in route.methods
        ]
        assert len(matches) == 1
        route = matches[0]
        assert route.endpoint is endpoints[path, method]
        assert route.status_code == status
        assert {dep.call for dep in route.dependant.dependencies} == dependencies
        assert route.endpoint.__module__ == "backend.routers.operations"
