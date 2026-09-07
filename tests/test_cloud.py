import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backfill.cloud import create_cloud_app
from backfill.cloud_store import CloudStore, digest

SITE = "http://testserver"


@pytest.fixture
def cloud(tmp_path):
    url = "sqlite:///" + str(tmp_path / "hosted.db")
    app = create_cloud_app(url, SITE, "initial-setup-secret")
    with TestClient(app, headers={"Origin": SITE}) as client:
        assert (
            client.post(
                "/cloud/setup",
                json={"token": "initial-setup-secret", "password": "a sufficiently long password"},
            ).status_code
            == 200
        )
        yield client, CloudStore(url), url


def pairing(client):
    response = client.post("/cloud/pairings")
    assert response.status_code == 200, response.text
    return response.json()["command"].split("'")[-2]


def test_password_sessions_survive_instance_restart_and_logout(cloud):
    client, store, url = cloud
    assert client.get("/v2/overview").status_code == 200
    with TestClient(create_cloud_app(url, SITE)) as other:
        other.cookies.update(client.cookies)
        assert other.get("/v2/overview").status_code == 200
    assert (
        client.post(
            "/cloud/setup",
            json={"token": "initial-setup-secret", "password": "different long password"},
        ).status_code
        == 409
    )
    assert client.post("/cloud/logout").status_code == 200
    assert client.get("/v2/overview").status_code == 401
    assert client.post("/cloud/login", json={"password": "wrong"}).status_code == 401
    assert (
        client.post("/cloud/login", json={"password": "a sufficiently long password"}).status_code
        == 200
    )
    assert store.one("SELECT password FROM owner")["password"] != "a sufficiently long password"


def test_pairing_retry_revocation_and_expiry(cloud):
    client, store, _ = cloud
    code = pairing(client)
    credential = secrets.token_urlsafe(32)
    body = {"code": code, "credential": credential, "name": "test worker"}
    result = client.post("/cloud/pair", json=body)
    assert result.status_code == 200, result.text
    assert client.post("/cloud/pair", json=body).json() == result.json()
    assert (
        client.post(
            "/cloud/pair", json={**body, "credential": secrets.token_urlsafe(32)}
        ).status_code
        == 401
    )
    assert client.post("/cloud/pairings").status_code == 409
    machine = result.json()["id"]
    assert store.one("SELECT hash FROM machines WHERE id=:id", id=machine)["hash"] == digest(
        credential
    )
    assert client.delete("/cloud/machine/" + machine).status_code == 200
    assert (
        client.post(
            "/cloud/sync",
            json={"snapshot": {}, "responses": []},
            headers={"Authorization": "Bearer " + credential},
        ).status_code
        == 401
    )
    code = pairing(client)
    with store.transaction() as db:
        db.execute(text("UPDATE pairings SET expires=0 WHERE hash=:hash"), {"hash": digest(code)})
    assert client.post("/cloud/pair", json={**body, "code": code}).status_code == 401


def test_queued_changes_replayed_until_ack_and_not_assigned_to_replacement(cloud):
    client, store, _ = cloud
    code = pairing(client)
    credential = secrets.token_urlsafe(32)
    machine = client.post(
        "/cloud/pair", json={"code": code, "credential": credential, "name": "worker"}
    ).json()["id"]
    task = {"title": "A task", "instructions": "Summarize a file"}
    key = secrets.token_hex(16)
    response = client.post("/v2/tasks", json=task, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    assert (
        client.post("/v2/tasks", json=task, headers={"Idempotency-Key": key}).json()
        == response.json()
    )
    assert (
        client.post(
            "/v2/tasks", json={**task, "title": "Another"}, headers={"Idempotency-Key": key}
        ).status_code
        == 409
    )
    headers = {"Authorization": "Bearer " + credential}
    body = {"snapshot": {"overview": {"tasks": [], "accounts": []}}, "responses": []}
    delivery = client.post("/cloud/sync", json=body, headers=headers)
    assert len(delivery.json()["commands"]) == 1
    assert client.post("/cloud/sync", json=body, headers=headers).json() == delivery.json()
    ack = {"id": key, "status": 200, "body": {"id": "local-task"}}
    assert (
        client.post("/cloud/sync", json={**body, "responses": [ack]}, headers=headers).json()[
            "commands"
        ]
        == []
    )
    assert client.get("/cloud/commands/" + key).json()["body"] == {"id": "local-task"}
    assert client.get("/cloud/machine").json()["machine"]["online"] is True
    with store.transaction() as db:
        db.execute(text("UPDATE machines SET seen=0 WHERE id=:id"), {"id": machine})
    assert client.get("/cloud/machine").json()["machine"]["online"] is False
    assert client.post("/v2/tasks", json=task, headers={"Idempotency-Key": key}).status_code == 200


def test_cross_origin_unknown_commands_and_unauthenticated_access(cloud):
    client, _, _ = cloud
    assert (
        client.post("/cloud/pairings", headers={"Origin": "https://attacker.example"}).status_code
        == 403
    )
    assert (
        client.post(
            "/v2/arbitrary", json={}, headers={"Idempotency-Key": secrets.token_hex(16)}
        ).status_code
        == 422
    )
    client.cookies.clear()
    for path in ("/cloud/machine", "/v2/overview", "/v2/tasks/anything"):
        assert client.get(path).status_code == 401
    assert client.get("/static/../../cloud.py").status_code == 404


def test_worker_snapshot_requires_credential(cloud):
    client, _, _ = cloud
    assert client.post("/cloud/sync", json={"snapshot": {}, "responses": []}).status_code == 401
