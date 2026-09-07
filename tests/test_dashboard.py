from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backfill.auth import read_secret
from backfill.config import Settings
from backfill.main import create_app


@pytest.fixture
def dashboard(tmp_path):
    root = tmp_path / "private"
    with TestClient(create_app(Settings(data_dir=root, meter_enabled=False))) as client:
        owner = {"Authorization": "Bearer " + read_secret(root / "owner.token")}
        yield client, owner


def test_private_session_origin_ticket_expiry_and_static_packaging(dashboard):
    client, owner = dashboard
    page = client.get("/")
    assert page.status_code == 200 and "Your work, moving forward." in page.text
    assert client.get("/static/app.js").status_code == 200
    assert "frame-ancestors" in page.headers["content-security-policy"]
    ticket = client.post("/v1/dashboard/ticket", headers=owner).json()["ticket"]
    response = client.post(
        "/v1/dashboard/session", json={"ticket": ticket}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=strict" in response.headers["set-cookie"]
    )
    assert client.get("/v1/status").status_code == 200
    assert client.post("/v1/dashboard/session", json={"ticket": ticket}).status_code == 401
    assert client.put("/v1/projects/a", json={"token_limit": 100}).status_code == 403
    assert (
        client.put(
            "/v1/projects/a", json={"token_limit": 100}, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/v1/projects/a", json={"token_limit": 100}, headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_both_accounts_and_dashboard_changes_persist(dashboard):
    client, owner = dashboard
    for provider in ("claude", "codex"):
        assert (
            client.put(
                "/v1/accounts/" + provider, headers=owner, json={"provider": provider}
            ).status_code
            == 200
        )
    assert (
        client.put("/v1/projects/research", headers=owner, json={"token_limit": 500}).status_code
        == 200
    )
    worker = client.put(
        "/v1/workloads/summary", headers=owner, json={"account": "claude", "priority": 20}
    ).json()["token"]
    assert (
        client.put(
            "/v1/workloads/summary/budget",
            headers=owner,
            json={"token_limit": 400, "project": "research"},
        ).status_code
        == 200
    )
    worker_headers = {"Authorization": "Bearer " + worker}
    assert (
        client.put(
            "/v1/workloads/summary/budget", headers=worker_headers, json={"token_limit": 999}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/workloads/summary/runs",
            headers=worker_headers,
            json={"provider": "claude", "request_id": "one"},
        ).json()["reason"]
        == "missing_observation"
    )
    until = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert client.put("/v1/pause", headers=owner, json={"paused_until": until}).status_code == 200
    result = client.get("/v1/status", headers=owner).json()
    assert {m["provider"] for m in result["meters"]} == {"claude", "codex"}
    assert all(a["policy"]["paused_until"] for a in result["accounts"])
    assert result["tasks"][0]["budget"]["project"] == "research"
    assert "token_hash" not in str(result)


def test_provider_binding_failure_does_not_mutate_account_policy(dashboard):
    client, owner = dashboard
    client.put("/v1/accounts/first", headers=owner, json={"provider": "codex"})
    response = client.put(
        "/v1/accounts/second", headers=owner, json={"provider": "codex", "policy": {"paused": True}}
    )
    assert response.status_code == 409
    assert [a["key"] for a in client.get("/v1/status", headers=owner).json()["accounts"]] == [
        "first"
    ]
