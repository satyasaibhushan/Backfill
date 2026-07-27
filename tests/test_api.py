from fastapi.testclient import TestClient

from backfill.config import Settings
from backfill.main import create_app


def task_payload() -> dict[str, object]:
    return {
        "title": "Tighten the release checks",
        "instructions": "Inspect the release workflow and add the missing validation.",
        "definition_of_done": "Relevant tests pass and the behavior is documented.",
        "repo_path": "/tmp/example-repository",
        "branch_name": "release-checks",
        "primary_branch": "main",
        "upstream_remote": "upstream",
        "priority": 80,
        "preferred_provider": "auto",
        "estimated_cost_percent": 12,
        "max_runtime_minutes": 45,
    }


def test_health_and_owner_identity(client: TestClient) -> None:
    health = client.get("/api/health")
    identity = client.get("/api/me")

    assert health.status_code == 200
    assert health.json()["authConfigured"] is True
    assert health.json()["schedulerConfigured"] is False
    assert identity.status_code == 200
    assert identity.json() == {"login": "owner@example.com"}


def test_task_lifecycle(client: TestClient) -> None:
    created = client.post("/api/tasks/", json=task_payload())

    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "queued"
    assert task["priority"] == 80

    paused = client.post(f"/api/tasks/{task['id']}/actions/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    retried = client.post(f"/api/tasks/{task['id']}/actions/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"

    tasks = client.get("/api/tasks/")
    assert tasks.status_code == 200
    assert [item["id"] for item in tasks.json()] == [task["id"]]


def test_prohibited_attribution_in_branch_is_rejected(client: TestClient) -> None:
    payload = task_payload()
    payload["branch_name"] = "codex-release-checks"

    response = client.post("/api/tasks/", json=payload)

    assert response.status_code == 422
    assert "prohibited attribution" in response.json()["detail"]


def test_unconfigured_auth_fails_closed(tmp_path) -> None:
    settings = Settings(
        auth_mode="tailscale",
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.get("/api/me")

    assert response.status_code == 503
    assert response.json()["detail"] == "Backfill authentication is not configured"


def test_tailscale_identity_is_exact_and_mutations_require_dashboard_origin(tmp_path) -> None:
    settings = Settings(
        auth_mode="tailscale",
        allowed_login="owner@example.com",
        public_origin="https://devbox.example.ts.net",
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        headers = {"Tailscale-User-Login": "owner@example.com"}

        assert test_client.get("/api/me", headers=headers).status_code == 200
        assert (
            test_client.post("/api/tasks/", headers=headers, json=task_payload()).status_code == 403
        )
        created = test_client.post(
            "/api/tasks/",
            headers={
                **headers,
                "Origin": "https://devbox.example.ts.net",
                "X-Backfill-Request": "dashboard",
            },
            json=task_payload(),
        )
        denied = test_client.get(
            "/api/me",
            headers={"Tailscale-User-Login": "someone-else@example.com"},
        )

    assert created.status_code == 201
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Tailscale login is not allowed"


def test_dashboard_is_served(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Backfill" in response.text
    assert "Dispatch now" in response.text
