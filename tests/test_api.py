import json

import pytest
from fastapi.testclient import TestClient

from backfill.auth import read_secret
from backfill.config import Settings
from backfill.main import create_app


@pytest.fixture
def api(tmp_path, setup, observe):
    service, _ = setup
    root = tmp_path / "private"
    with TestClient(create_app(Settings(data_dir=root), service)) as client:
        owner = {"Authorization": "Bearer " + read_secret(root / "owner.token")}
        response = client.put(
            "/v1/workloads/worker", headers=owner, json={"account": "account", "priority": 50}
        )
        assert response.status_code == 200
        worker = {"Authorization": "Bearer " + response.json()["token"]}
        yield client, owner, worker


def test_owner_and_worker_scopes(api):
    client, owner, worker = api
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Tailscale-User-Login": "owner"}).status_code == 401
    assert client.get("/v1/status", headers=owner).status_code == 200
    assert client.get("/v1/status", headers=worker).status_code == 403
    assert client.get("/v1/workloads/worker", headers=worker).status_code == 200
    assert client.get("/v1/workloads/bulk", headers=worker).status_code == 403
    assert (
        client.put(
            "/v1/workloads/worker", headers=worker, json={"account": "account", "priority": 100}
        ).status_code
        == 403
    )
    assert (
        client.post("/v1/accounts/account/observations", headers=worker, json={}).status_code == 403
    )
    assert client.put("/v1/accounts/account", headers=worker, json={}).status_code == 403


def test_api_full_grant_report_release_and_credential_rotation(api):
    client, owner, worker = api
    acquired = client.post(
        "/v1/workloads/worker/acquire",
        headers=worker,
        json={"request_id": "unit-1", "costs": {"session": 5, "weekly": 1}},
    )
    assert acquired.status_code == 200
    grant = acquired.json()["grant_id"]
    path = f"/v1/workloads/worker/grants/{grant}"
    assert client.get(path, headers=worker).json()["can_spend"]
    assert (
        client.post(path.replace("worker", "bulk") + "/release", headers=worker).status_code == 403
    )
    reported = client.post(
        path + "/report",
        headers=worker,
        json={"report_id": "one", "consumed": {"session": 3, "weekly": 0.5}},
    )
    assert reported.status_code == 200 and reported.json()["state"] == "closed"
    result = client.get("/v1/workloads/worker", headers=worker).json()
    assert result["windows"]["session"]["unconfirmed_usage"] == 3
    assert "token" not in json.dumps(result)
    rotated = client.post("/v1/workloads/worker/rotate-token", headers=owner).json()["token"]
    assert client.get("/v1/workloads/worker", headers=worker).status_code == 401
    assert (
        client.get(
            "/v1/workloads/worker", headers={"Authorization": f"Bearer {rotated}"}
        ).status_code
        == 200
    )


def test_workers_cannot_change_priority_in_acquire(api):
    client, _, worker = api
    response = client.post(
        "/v1/workloads/worker/acquire",
        headers=worker,
        json={"request_id": "x", "costs": {"session": 1}, "priority": 100},
    )
    assert response.status_code == 422
