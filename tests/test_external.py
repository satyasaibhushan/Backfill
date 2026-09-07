import hashlib

import pytest
from test_tasks import tasks as task_fixture

from backfill.external import External
from backfill.governor import Governor
from backfill.quota import QuotaError
from backfill.schemas import RunStart, RunUsage
from backfill.tasks import ProjectInput, TaskAction


@pytest.fixture
def tasks(tmp_path):
    return task_fixture.__wrapped__(tmp_path)


def connected(fixture):
    app, clock, observe = fixture
    governor = Governor(app.quota)
    service = External(app, governor)
    project = app.project(ProjectInput(name="Shared work", allowance=5))
    secret = "test-secret-not-live"
    grant = dict(
        id="app-one",
        project=project["id"],
        hash=hashlib.sha256(secret.encode()).hexdigest(),
        revoked=0,
    )
    service.sync([grant])
    return app, clock, observe, governor, service, service.identity(secret)


def register(service, principal, request="request-one", **values):
    return service.register(
        principal, request, {"title": "Investigate", "instructions": "Read files", **values}
    )


def test_scope_idempotency_and_external_queue_ownership(tasks):
    app, _, _, _, service, principal = connected(tasks)
    job = register(service, principal)
    assert register(service, principal)["id"] == job["id"]
    assert job["project"] == principal["project"]
    assert app.candidates() == []
    with pytest.raises(QuotaError):
        register(service, principal, instructions="Different work")
    with pytest.raises(QuotaError):
        service.record({"id": "another-app"}, job["id"])
    service.sync([])
    with pytest.raises(QuotaError):
        service.identity("test-secret-not-live")


def test_shared_project_cap_stops_active_guard_and_later_tasks(tasks):
    app, clock, observe, governor, service, principal = connected(tasks)
    first = register(service, principal, allowance=10)
    permit = service.start(principal, first["id"])
    assert permit["decision"] == "granted"
    run = governor.start(permit["workload"], RunStart(provider="claude", request_id="native-one"))
    assert run["decision"] == "granted"
    clock[0] += 2
    observe("claude", 8)
    service.enforce(permit["workload"])
    result = governor.report(permit["workload"], run["run_id"], RunUsage(sequence=0, tokens=10))
    assert not result["can_spend"]
    second = register(service, principal, "request-two")
    assert service.start(principal, second["id"])["decision"] == "wait"
    assert app.get(first["id"])["consumption"]["windows"]


def test_revocation_and_pause_are_applied_to_running_guard(tasks):
    app, _, _, governor, service, principal = connected(tasks)
    job = register(service, principal)
    permit = service.start(principal, job["id"])
    run = governor.start(permit["workload"], RunStart(provider="claude", request_id="native-one"))
    app.action(job["id"], TaskAction(action="pause"))
    service.enforce(permit["workload"])
    assert not governor.status(permit["workload"], run["run_id"])["can_spend"]


def test_lost_client_is_not_silently_reexecuted(tasks):
    app, clock, _, _, service, principal = connected(tasks)
    job = register(service, principal)
    assert service.start(principal, job["id"])["decision"] == "granted"
    clock[0] += 50
    recovered = register(service, principal)
    assert recovered["state"] == "failed"
    assert "Review saved progress" in recovered["reason"]
    assert len(app.get(job["id"])["attempts"]) == 1


def test_local_selection_is_scoped_and_survives_cloud_sync(tasks):
    app, _, _, _, service, _ = connected(tasks)
    other = app.project(ProjectInput(name="Another budget", allowance=20))
    value = dict(project=other["id"], key="my-application", credential="x" * 48)
    grant = service.connect_local(value)
    assert service.connect_local(value) == grant
    service.sync([])
    principal = service.identity(value["credential"])
    assert principal["project"] == other["id"]
    assert register(service, principal)["project"] == other["id"]
    with pytest.raises(QuotaError):
        service.connect_local({**value, "project": "deleted-project"})
