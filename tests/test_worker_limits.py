"""B4 Temporal slot limits are validated and applied to the real worker."""

import pytest
from pydantic import ValidationError

from backend.settings import Settings
from backend.workers import incident_worker


def test_worker_slot_limits_are_bounded():
    assert Settings().worker_workflow_slots == 8
    assert Settings().worker_activity_slots == 8
    with pytest.raises(ValidationError):
        Settings(worker_workflow_slots=1)
    with pytest.raises(ValidationError):
        Settings(worker_activity_slots=0)
    with pytest.raises(ValidationError):
        Settings(worker_activity_slots=65)


@pytest.mark.asyncio
async def test_real_worker_startup_receives_configured_slot_limits(monkeypatch):
    seen = {}
    settings = Settings(
        worker_workflow_slots=4, worker_activity_slots=3,
    )

    class FakeWorker:
        def __init__(self, client, **kwargs):
            seen["client"] = client
            seen.update(kwargs)

        async def run(self):
            return None

    async def connect(_settings):
        assert _settings is settings
        return "test-temporal-client"

    async def heartbeat(role):
        assert role == "incident_worker"

    monkeypatch.setattr(incident_worker, "Worker", FakeWorker)
    monkeypatch.setattr(incident_worker, "connect_temporal", connect)
    monkeypatch.setattr(incident_worker, "get_settings", lambda: settings)
    monkeypatch.setattr(incident_worker, "heartbeat_loop", heartbeat)
    await incident_worker.main()
    assert seen["client"] == "test-temporal-client"
    assert seen["max_concurrent_workflow_tasks"] == 4
    assert seen["max_concurrent_activities"] == 3
    assert seen["task_queue"] == settings.temporal_task_queue
